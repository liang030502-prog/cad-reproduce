# 流水线细节

主文件 `SKILL.md` 给流程；本文档给每一步的完整调用、判据和排错。

## 目录

1. [环境检查](#1-环境检查)
2. [抽取](#2-抽取-extractpy)
3. [标注推断](#3-标注推断-infer_dimspy)
4. [生成](#4-生成-stage1_buildpy)
5. [门禁](#5-门禁-tracepy--comparepy)
6. [AutoCAD 验收](#6-autocad-验收-accadpy)
7. [排错对照表](#7-排错对照表)

---

## 1. 环境检查

```bash
python -c "import ezdxf, pymupdf, PIL, numpy; print('deps ok')"
python -c "import ezdxf; print('ezdxf', ezdxf.__version__)"     # 本流程在 1.4.4 上验证
```

AutoCAD 位置**必须查注册表**，不要假设在 C 盘：

```powershell
Get-ItemProperty 'HKLM:\SOFTWARE\Autodesk\AutoCAD\R24.0\ACAD-4101:804' |
    Select-Object ProductName, AcadLocation
```

工作目录可写性**与文件沙箱无关**，是 Windows ACL 的事：

```powershell
(Get-Acl '<dir>').Access | Where-Object { $_.IdentityReference -match "$env:USERNAME" }
```

不可写时（需管理员）：

```
icacls "<dir>" /grant "<DOMAIN\user>:(OI)(CI)M" /T /C
```

---

## 2. 抽取（`extract.py`）

```bash
python scripts/extract.py "$JOB/source.pdf" -o "$JOB/extract.json" [--dpi 150]
```

产出三块：

| 键 | 内容 | 用途 |
|---|---|---|
| `paths` | 每条矢量路径：`color`(描边) / `fill`(填充) / `width` / `rect` / `items` | 几何与配色的唯一来源 |
| `spans` | 每个文字 span：文字 / bbox / 字号 / 颜色 / 方向 | 重建 TEXT |
| `raster` | 光栅化后的颜色份额 | 配色的**独立佐证** |

**先读输出的"颜色组合统计"**，确认每种颜色的角色。特别注意：

- `stroke=none fill=X` 的路径是"只有填充"的形状
- `width=0` 的路径是零宽描边（PDF 里常见）

**不要**假设"描边色就是可见色"——有的 PDF 有不可见文本层（`3 Tr`）。

---

## 3. 标注推断（`infer_dims.py`）

```bash
python scripts/infer_dims.py "$JOB/extract.json" -o "$JOB/dims.json"
```

### 判据（全部来自源图实测）

| 概念 | 判据 |
|---|---|
| 尺寸线 | **某个颜色的**长直线段（默认绿色，实测本图纸绿色才是尺寸线） |
| 界线 | 与尺寸线**垂直**的短线段 |
| 特征位置 | 界线**远离尺寸线的那一端**（决定偏移方向与符号） |
| 一条链的切分 | 同一直线上共线线段之间的**箭头间隙** |
| 数值归属 | 数字标签**最近的**那一段（唯一归属，不能共享） |

### 输出

`proposals[]` 每项含 `axis` / `p1` / `p2` / `line_pos` / `offset_sign` / `value` /
`label_bbox` / `confidence` / `notes`。

**只保留 high 与 medium**；low 的记录在案但不画。若某个标注是你预期的却落到 low，
看 `notes` 里的原因（缺标签 / 缺一端界线 / 多候选标签）。

---

## 4. 生成（`stage1_build.py`）

```bash
python scripts/stage1_build.py "$JOB/extract.json" "$JOB/dims.json" \
    -o "$JOB/repro.dxf" --json "$JOB/build.json" \
    [--min-confidence medium] [--associative] [--mtext] [--no-dimensions]
```

### 四项原生实体的写法

**实心图形**（`_reads_as_solid` 判定 + `_emit_solid`）：

```python
p = msp.add_lwpolyline([...], close=True)      # ❌ 不填充，会空心
h = msp.add_hatch(dxfattribs=attrs)            # ✅
h.set_solid_fill(color=aci)
h.paths.add_edge_path().add_arc(center, radius, 0, 360)   # 圆
msp.add_circle(center, radius, dxfattribs=attrs)          # 叠加真圆
```

**活标注**：

```python
d = msp.add_linear_dim(base=base, p1=p1, p2=p2, dimstyle=name,
                       text="<>", dxfattribs={"layer": "DIMENSION"})
d.set_text("<>")            # 关联：AutoCAD 自己测量
d.render()                  # 必须！否则没有几何块
```

`base` 在尺寸线上；`p1`/`p2` 在**特征上**（不是尺寸线端点）。

**文字**：`msp.add_text(...)`，样式字体用**家族名**。

**颜色**：`{黑:250, 红:1, 黄:2, 绿:3, 青:4, 品红:6}`——六色都有精确 ACI，无需真彩色。

### 必须读的构建报告字段

- `dimensions_emitted` —— 应等于 dims.json 里保留的数量
- `suppressed.dimension_geometry` / `glyph_outline` / `dimension_text`
- `fonts_resolved` —— 确认解析到了真实字体文件
- `lineweight_substitutions` —— 请求值与写入值不一致的会被列出

---

## 5. 门禁（`trace.py` + `compare.py`）

```bash
python scripts/trace.py "$JOB/extract.json" --dims "$JOB/dims.json" \
    --out-dir "$JOB/trace" --dpi 300
python scripts/compare.py "$JOB/trace/trace_source.png" "$JOB/trace/trace_generated.png" \
    -o "$JOB/out" --dpi 300 --band-mm 0.12 --json "$JOB/cmp.json"
```

### 为什么用自建 tracer 而不是库渲染器

| | 库渲染器 | AutoCAD 出图 | tracer |
|---|---|---|---|
| 线宽忠实 | ❌ 被夹在 1–4px | ✅ | ✅ 直接按毫米 |
| 不裁边 | ✅ | ❌ 按视口裁 | ✅ |
| 快 | ✅ | ❌ 起进程 | ✅ |
| 中文字形 | ❌ 空方块 | ✅ | ⚠️ 近似 |

**关键**：tracer 用**同一个**光栅化器画源几何和成品几何，所以线宽语义天然一致，
差异必然来自图纸本身。

### 判据与阈值

| 指标 | 阈值 | 含义 |
|---|---|---|
| `geometric_agreement` | ≥ 0.97 | 带内一致率：墨水是否落在彼此 ±band 内 |
| 每色 `delta` | ≤ 0.03 | 颜色份额差 |
| 每色 `iou` | ≥ 0.70 | 位置交叠率（与线宽无关，是几何位置的硬指标） |

**不要用墨水面积当判据**——两种光栅化器抗锯齿范围不同，面积差恒在 25% 量级。

### 看差异图

`out/cmp_difference.png`：**灰=一致，蓝=成品多余，红=源图有而成品没有**。
红块集中处就是缺失的几何。

### 收敛曲线诊断法

加宽 `--band-mm` 重跑：若一致率**封顶**，剩下的是结构差异；
若持续上升，剩下的是线宽/抗锯齿差异。

---

## 6. AutoCAD 验收（`accad.py`）

```bash
python scripts/accad.py "$JOB/repro.dxf" -o "$JOB/acad" \
    --audit --dwg --export --json "$JOB/acad.json"
```

| 开关 | 作用 | 看什么 |
|---|---|---|
| `--audit` | 在 AutoCAD 里数实体、取范围、列字体 | 实体数合理、无代理实体、extents 与源图一致 |
| `--dwg` | SAVEAS 成 DWG | 文件 >4KB |
| `--export` | `-EXPORT` 出 PDF | 字体不是替换字体、中文可提取 |
| `--plot` | `-PLOT` 彩色出图 | 可能挂起，优先用 `--export` |

**这是唯一能判定字体和填充的环节**，但它会裁边，所以不做像素比对。

---

## 7. 排错对照表

| 症状 | 病因 | 处置 |
|---|---|---|
| 中文变空方块 | 字体名解析失败，静默回退到无 CJK 的字体 | 改用家族名（`SimSun` 而非 `simsun.ttc`）；查 `build.json` 的 `fonts_resolved` |
| 实心图形是空心 | 用了闭合多段线；或 `$FILLMODE=0` | 改 `HATCH`+`set_solid_fill`，并设 `$FILLMODE=1` |
| 数字消失 | 删了字形轮廓但没补 TEXT | 两者必须成对 |
| 数字出现两遍 | 补了 TEXT 但没删轮廓 | 同上 |
| 标注值是偏移距离 | `p1`/`p2` 传了尺寸线端点 | 改传特征上的点 |
| 标注镜像到错误一侧 | 界线方向取反了 | 取**远离尺寸线**的那一端 |
| 同一数字出现在多处 | 标签被多个跨度共享 | 改成唯一归属（最近者胜） |
| 图框/大片几何丢失 | 抑制区用了整条尺寸线的外接矩形，或按 path 而非 item 判断 | 抑制区做薄带；逐 item 判断 |
| 一张图里几千个无用 HATCH | "笔宽吞没"判据把退化碎片也算了 | 要求 ≥3 顶点、两个方向都有面积 |
| 门禁几何一致率上不去 | 用了库渲染器（线宽不忠实） | 换 `trace.py` |
| 门禁颜色份额差很大 | 同上；或配色规则错 | 换 tracer；回到第 2 步核对颜色角色 |
| AutoCAD 审计返回 0 实体 | 脚本没跑成（退出码不可信） | 看 `.scr.log`；确认 `.scr` 是 GBK 编码 |
| accoreconsole 挂起 | 提示应答错、或 UTF-8 脚本 | 用 `-EXPORT`；脚本存 GBK；加超时 |
| 出图 PDF 内容被裁 | `-EXPORT` 按视口裁 | 正常现象；像素比对改用 tracer |
| 构建失败但下游用了旧文件 | 失败没让下游失效 | 构建前删目标文件；门禁前检查 mtime |
