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

产出四块：

| 键 | 内容 | 用途 |
|---|---|---|
| `paths` | 每条矢量路径：`color`(描边) / `fill`(填充) / `width` / `rect` / `items` / `dashes` | 几何与配色的唯一来源 |
| `spans` | 每个文字 span：文字 / bbox / 字号 / 颜色 / 方向 | 重建 TEXT |
| `linetypes` | 每种虚线图案：`pattern_pt`（划线/间隔，源图点）与路径数 | 生成器据此定义真 DXF linetype |
| `raster` | 光栅化后的颜色份额 + 使用的调色板 | 配色的**独立佐证** |

`stats` 里另外三个必须看的数：`dashed_paths`（虚线路径数）、`linetype_counts`、
`colours_outside_named_set`（六色之外的颜色，会被写成真彩色）。

**先读输出的"颜色组合统计"**，确认每种颜色的角色。特别注意：

- `stroke=none fill=X` 的路径是"只有填充"的形状
- `width=0` 的路径是零宽描边（PDF 里常见）
- `dashes` 是**字符串**（`"[ 6 3 ] 0"`），实线是 `"[] 0"`，无描边是 `None`

**不要**假设"描边色就是可见色"——有的 PDF 有不可见文本层（`3 Tr`）。

---

## 3. 标注推断（`infer_dims.py`）

```bash
python scripts/infer_dims.py "$JOB/extract.json" -o "$JOB/dims.json" \
    [--config cad-reproduce.yaml] [--colour green] [--allow-empty] \
    [--label-min-size 5.2]
```

### 第一步：确认颜色角色

脚本**自动检测**哪种颜色承担尺寸机构，并把全部候选的证据表打印出来
（箭头数 / 被界线夹住的尺寸线条数 / 同色数字标签数 / 得分）。三种结局：

| 结局 | 表现 | 处置 |
|---|---|---|
| 你要的角色成立 | 打印 `(confirmed by the geometry)` | 继续 |
| 你要的角色不成立 | **退出码 2**，报"the dimension colour role is wrong" | 改 `--colour` 或 yaml 的 `dimensions.colour` |
| 没有任何角色成立 | 提示"这张图看起来没有尺寸"，退出码 0 | 确实没有尺寸；加 `--allow-empty` 消掉提示 |
| 几何区分不出角色（单色图纸常见） | 照常推理，同时打印 ambiguous 的原因 | 保险起见把答案写进 yaml |

### 第二步：看尺寸值字号（`label text floor`）

**这是第二张真实图纸才暴露的一步。** 尺寸值的字号跟着绘图比例走，所以它和箭头一样
是**每张图要重新量的绝对量**：参考图 5.49pt，钢构图的主尺寸链 2.86pt —— 用前者的
门槛会把后者**整条排除**（实测：9 个标注 vs 43 个）。

脚本默认**逐档试**该图印数字的每个字号，选哪一档的"数值 ÷ 画长"能配成**最大的比例簇**，
并打印对照表：

```
label text floor : 2.86 pt  (MEASURED; the configured 5.2 pt would have changed the result)
floor pt  usable  largest cluster  share  consistent
2.86      43      29               67%    36
3.44       9       3               33%     5
3.76       9       3               33%     5
5.20       9       3               33%     5
```

**不要按字号大小猜。** 钢构图有四层数字（2.86 主链 / 3.44 材料表 / 3.76 标题栏 /
5.24 详图焊缝），最像尺寸值的那层（5.24）**是错的**；参考图上"取最大"恰好对。
判据只能是配对自洽。

- `changed` = 选中的档 ≠ 配置档；`outcome_changed` = **结果真的不同**。
  参考图上 `changed=True` 但 `outcome_changed=False`（两种档给同样的 24 个标注），
  所以报告不会虚报"配置档是错的"。
- 要钉死：`--label-min-size 5.2` 或 yaml 里 `label_floor_search: false`。
- 代价：活 DIMENSION 的数值文字由 CAD 摆位，不落在源图 span 原处（实测偏移约 0.5mm）。
  钢构图 9→43 个标注的代价是几何一致率 0.9944→0.9843，门禁仍通过。

**注意**：数字标签的颜色**不是**判断依据——标签是文本，文本颜色与它标注的线
没有必然关系。本图纸 25 个尺寸数字的文本 span 是黑色，导致 black 的证据反而更强。
所以检测判据用"箭头"和"被界线夹住的尺寸线"。

### 判据（全部来自源图实测，全部可在 yaml 里改）

| 概念 | 判据 |
|---|---|
| 尺寸线 | **某个颜色的**长直线段（默认绿色，实测本图纸绿色才是尺寸线） |
| 界线 | 与尺寸线**垂直**的短线段（`ext_min_len_pt` 以上） |
| 特征位置 | 界线**远离尺寸线的那一端**（决定偏移方向与符号） |
| 一条链的切分 | 同一直线上共线线段之间的**箭头间隙** |
| 数值归属 | 数字标签**最近的**那一段（唯一归属，不能共享） |
| 箭头 | 小实心形状：item 数 + 外接框（默认 6 个 item、5.4×1.32pt） |
| 数值标签 | 纯数字且字号 ≥ `label_min_size_pt`（把标题栏字段排除掉） |

### 输出

`proposals[]` 每项含 `axis` / `p1` / `p2` / `line_pos` / `offset_sign` / `value` /
`label_bbox` / `confidence` / `notes`；顶层含 `dimension_colour` 与 `colour_check`
（检测结论、证据表、是否 ambiguous）。

**只保留 high 与 medium**；low 的记录在案但不画。若某个标注是你预期的却落到 low，
看 `notes` 里的原因（缺标签 / 缺一端界线 / 多候选标签）。

**没有任何 high/medium 时会失败**（退出码 2），除非 `--allow-empty`。
这是故意的：0 个标注以前看起来和"这张图没有尺寸"一模一样。

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
    -o "$JOB/out" --dpi 300 --band-mm 0.12 --json "$JOB/cmp.json" \
    --extract "$JOB/extract.json"
```

`--extract` 让颜色分类用**源图自己的调色板**（`cadkit.raster_palette`）而不是固定
八色。不加它，源图里六色之外的颜色会被归到最近的邻近色，颜色份额与 IoU 衡量的
就是调色板而不是图纸。

### 为什么用自建 tracer 而不是库渲染器

| | 库渲染器 | AutoCAD 出图 | tracer |
|---|---|---|---|
| 线宽忠实 | ❌ 被夹在 1–4px | ✅ | ✅ 直接按毫米 |
| 不裁边 | ✅ | ❌ 按视口裁 | ✅ |
| 快 | ✅ | ❌ 起进程 | ✅ |
| 中文字形 | ❌ 空方块 | ✅ | ⚠️ 近似 |

**关键**：tracer 用**同一个**光栅化器画源几何和成品几何，所以线宽语义天然一致，
差异必然来自图纸本身。虚线同理：两侧都按 `extract.json` 的 linetype 表画。

### 门禁的能力边界（必须知道）

源侧影像**不是 PDF 渲染**，而是拿 `extract.json` 自己画的；而且 `_reads_as_solid()`
在 `trace.py` 与 `stage1_build.py` 里是**同一套判据的两份拷贝**。
→ 实心判错、尺寸裁剪判错这类**系统性错误两边会一起犯，一致率照样 PASS**。
门禁能证明的是"生成器没有漏画、没有错位"；**不能**证明"抽取与判读是对的"。

因此换新图纸时：门禁通过 ≠ 图纸对。至少另做一次独立对照
（AutoCAD `--export` 出图看字体/填充/实心、人工确认颜色角色与虚线表）。

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
| **虚线画成实线** | `dashes` 抽了但生成器没读；或它本来就是 `"[] 0"` | 查 `extract.json` 的 `linetypes`；生成器侧看 `build.json` 的 `linetypes.defined` |
| **虚线长度不对** | DXF 线型首元素是总长，被当成第一段虚线；或漏了 pt→mm 换算 | 读回 DXF，断言 group 49 的元素个数与毫米长度 |
| **蓝线变黑线 / 颜色整体偏** | 颜色被量化到六个名字里 | 查 `stats.colours_outside_named_set`；调色板外必须写真彩色 |
| **门禁报告一个奇怪的邻近色** | `compare.py` 没传 `--extract`，用了固定八色调色板 | 加 `--extract "$JOB/extract.json"` |
| **尺寸一个都没推断出来** | 颜色角色不对（旧版会静默返回 0） | 看 `infer_dims` 打印的证据表；把答案写进 `dimensions.colour` |
| `infer_dims` 退出码 2 | 角色不对，或没有任何 high/medium 标注 | 按 stderr 的提示改 `--colour` / yaml；确实无尺寸时用 `--allow-empty` |
| 虚线圆弧仍是实线 | DXF 样条曲线不支持线型 | 已知限制；看 `build.json` 的 `dashed_curve_not_dashed` |
| 门禁 PASS 但图纸明显不对 | 门禁与被测对象共模（见第 5 节"能力边界"） | 用 AutoCAD `--export` 出图独立核对；新图纸必须另做一次对照 |
