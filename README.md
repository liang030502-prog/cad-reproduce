# cad-reproduce

把 PDF 工程图复刻成 **AutoCAD 可编辑的 DXF/DWG**，并用自检门禁量化验证几何与颜色。

实战入口。读这个文件就能跑起来。

---

## 1. 环境要求

| 组件 | 版本 | 用途 | 检查命令 |
|---|---|---|---|
| Python | 3.10+（本机 3.14.4） | 全部脚本 | `python -V` |
| ezdxf | 1.4.4（已验证） | 生成 DXF | `python -c "import ezdxf;print(ezdxf.__version__)"` |
| PyMuPDF | 1.28.2（已验证） | 读 PDF | `python -c "import pymupdf;print(pymupdf.version)"` |
| Pillow | 12.x | 光栅化（门禁用） | `python -c "import PIL;print(PIL.__version__)"` |
| numpy | 2.x | 门禁计算 | `python -c "import numpy;print(numpy.__version__)"` |
| PyYAML | 任意 | 读配置 | `python -c "import yaml"` |
| AutoCAD | 2021 简体中文（本机 `D:\AutoCAD 2021`） | 权威验收、出图、DWG | 见下 |

一次性检查：

```bash
python -c "import ezdxf, pymupdf, PIL, numpy, yaml; print('所有依赖 OK')"
```

**AutoCAD 位置必须查注册表**，不要假设在 C 盘：

```powershell
Get-ItemProperty 'HKLM:\SOFTWARE\Autodesk\AutoCAD\R24.0\ACAD-4101:804' |
    Select-Object ProductName, AcadLocation
```

把结果填进 `cad-reproduce.yaml` 的 `acad_home`。

**没有 AutoCAD 也能用**：跳过第 6 步即可。门禁（第 5 步）不依赖 AutoCAD。

---

## 2. 快速开始（把 `$JOB` 换成你的作业目录）

```powershell
$SK  = '<skill 目录>\scripts'
$JOB = '<作业目录>'
$env:PYTHONIOENCODING = 'utf-8'      # 必需，否则中文输出会乱码
New-Item -ItemType Directory -Force $JOB | Out-Null
Copy-Item '<你的图纸>.pdf' "$JOB\source.pdf"

# 1 抽取
python "$SK\extract.py" "$JOB\source.pdf" -o "$JOB\extract.json"

# 2 推断活标注（脚本会检测"哪种颜色是尺寸线"；角色不对会报错，不会静默返回 0 个）
python "$SK\infer_dims.py" "$JOB\extract.json" -o "$JOB\dims.json"

# 3 生成
python "$SK\stage1_build.py" "$JOB\extract.json" "$JOB\dims.json" `
    -o "$JOB\repro.dxf" --json "$JOB\build.json"

# 4 门禁（自建忠实光栅化 + 比对）
python "$SK\trace.py" "$JOB\extract.json" --dims "$JOB\dims.json" `
    --out-dir "$JOB\trace" --dpi 300
python "$SK\compare.py" "$JOB\trace\trace_source.png" "$JOB\trace\trace_generated.png" `
    -o "$JOB\out" --dpi 300 --json "$JOB\cmp.json" --extract "$JOB\extract.json"
# 退出码 0 = 通过；--extract 让配色判据用源图自己的调色板
# （不加它，六色之外的源图颜色会被归到邻近色，判据衡量的是调色板而不是图纸）

# 5 AutoCAD 权威验收（可跳过）
python "$SK\accad.py" "$JOB\repro.dxf" -o "$JOB\acad" `
    --audit --dwg --export --json "$JOB\acad.json"
```

**门禁不通过就不要交付。** 看 `$JOB\out\cmp_difference.png`：
**灰=一致，蓝=成品多余，红=源图有而成品没有**。

---

## 3. 每一步在干什么、看什么

| 步 | 脚本 | 输出 | 必须看的 |
|---|---|---|---|
| 1 | `extract.py` | `extract.json` | "颜色组合统计"、**linetype 表**（虚线图案）、`colours_outside_named_set`——确认每种颜色承担什么角色、有哪些虚线 |
| 2 | `infer_dims.py` | `dims.json` | **颜色角色检测结论**（不成立会退出码 2）＋置信度分布；**只保留 high/medium** |
| 3 | `stage1_build.py` | `repro.dxf` + `build.json` | `dimensions_emitted`、`fonts_resolved`、`suppressed`、`linetypes.defined` |
| 4 | `trace.py`+`compare.py` | `trace/*.png` `out/cmp_*.png` | 三项判据 + 差异图；**注意门禁的能力边界（见下）** |
| 5 | `accad.py` | `.dwg`、`_export.pdf`、审计 | 实体数、`extents`、**PDF 字体不是替换字体** |

### 门禁能证明什么、不能证明什么

能：生成器**没有漏画、没有错位**（像素级，带 0.12mm 容差）。

**不能**：抽取与判读本身是否正确。源侧影像是拿 `extract.json` 自己画的，
实心判据在 `trace.py` 与 `stage1_build.py` 里是同一套规则的两份拷贝——
**规则错了两边一起错，一致率照样 0.98 PASS**。
所以换新图纸时，门禁通过不等于图纸对，必须另做一次独立对照
（AutoCAD `--export` 出图、人工确认颜色角色与虚线表）。

### 自检套件（改动脚本后按顺序跑）

```powershell
python evals\test_repository.py     # 1. 仓库本身：文档链接、Skill 可加载、无机器/图纸特征串
python evals\test_regressions.py    # 2. 流水线行为：四类历史缺陷不再复发
python evals\test_arc_recognition.py # 3. 圆弧识别：真 ARC/CIRCLE 而不是一串 SPLINE
```

**三者全过 = 退出码 0。**

- `test_repository.py` 检查的是**仓库这个产物**：SKILL.md 的 frontmatter 能被解析、
  文档里每个文件名引用都存在（防"README 指向已删除的文件"）、没有个人/机器特征串、
  每个被文档点名的脚本都能跑且接受文档里写的参数、**文档承诺的能力在代码里真的有接线**
  （例如虚线必须同时出现在 `extract`/`stage1_build`/`trace` 三处，缺一处就是假的），
  最后把上面两套行为测试也跑一遍。
- `test_regressions.py` 造两张最小 PDF 跑通整条流水线，断言四类**门禁看不见**的
  历史缺陷不再复发：虚线进 DXF 成为真 linetype（长度按毫米换算正确）、六色外的颜色
  写真彩色而不是黑、错误的颜色角色退出码 2、追绘的尺寸用检测到的颜色。
- `test_arc_recognition.py` 断言 PDF 里的三次贝塞尔被识别成真 `ARC`/`CIRCLE`
  （中心、半径、方向都要对），自由曲线仍保持 `SPLINE`，且合并后的样条是精确的。

**为什么仓库也要有测试**：这个仓库已经出过"README 指向已删除文件"、
"一个新分支合进来把上一个修复静默回退"、"文档承诺虚线圆弧支持而记账代码已被丢掉"
这三类事故——它们都不是流水线行为问题，行为测试永远抓不到。

### 门禁判据

| 指标 | 阈值 | 含义 |
|---|---|---|
| `geometric_agreement` | ≥ 0.97 | 带内一致率（墨水是否落在彼此 ±0.12mm 内） |
| 每色 `delta` | ≤ 0.03 | 颜色份额差 |
| 每色 `iou` | ≥ 0.70 | 位置交叠率（与线宽无关） |

### 常用可选参数

```powershell
# 推断：直接指定尺寸线的颜色（不确定时省略，脚本会检测并打印证据表）
python "$SK\infer_dims.py" ... --colour green

# 推断：这张图确实没有尺寸，接受空结果（否则空结果按失败处理，退出码 2）
python "$SK\infer_dims.py" ... --allow-empty

# 推断：从 yaml 读全部阈值（颜色角色 / 字高 / 箭头签名 / 容差）
python "$SK\infer_dims.py" ... --config "$SK\..\cad-reproduce.yaml"

# 门禁：用源图自己的调色板做颜色分类（六色之外的图纸必须加）
python "$SK\compare.py" ... --extract "$JOB\extract.json"

# 生成：把标注交给 CAD 自动测量（仅当图纸几何与数字自洽时才用）
python "$SK\stage1_build.py" ... --associative

# 生成：多行文字用 MTEXT（库渲染器会画成空方块，需用 AutoCAD 出图验证）
python "$SK\stage1_build.py" ... --mtext

# 生成：不画活标注（退回手画几何，用于对照）
python "$SK\stage1_build.py" ... --no-dimensions

# 生成 + 门禁：置信度门槛必须一致，否则两边画的标注不同
python "$SK\stage1_build.py" ... --min-confidence low
python "$SK\trace.py" ... --min-confidence low

# 门禁：诊断"剩下的是结构差异还是线宽差异"
python "$SK\compare.py" ... --band-mm 0.30    # 若一致率封顶 → 结构差异
```

---

## 4. 提示词（需要视觉模型时用）

**默认全程不需要视觉模型**——颜色、实心识别、漏画多画、标注数值、线宽全部由测量判定，
比"看一眼"可靠。**只在原图语义歧义时才用**（详见 `SKILL.md` 的最后一节）。

### 4.1 环境准备（Codex CLI，本机未安装）

```powershell
npm install -g @openai/codex      # 或按官方文档安装
codex --version
```

**用法要点：提示词必须在 `-i` 之前。**

```powershell
codex exec "<提示词>" -i "<图片路径>" -o "<输出文件.md>"
```

图片要先裁到目标区域并放大（视觉模型会压缩大图，小字会糊）：

```powershell
# 用 PyMuPDF 裁一块并放大 4 倍
python -c @"
import pymupdf
pg = pymupdf.open(r'<source.pdf>')[0]
# (x0, y0, x1, y1) 用 PDF 点，见 extract.json 里 span 的 bbox
pg.get_pixmap(dpi=300, clip=pymupdf.Rect(160, 190, 260, 260)).save(r'<crop.png>')
"@
```

### 4.2 三类歧义的提示词模板

**模板 A：读不出来的数字**（对应本流程遇到的 50 个 `fs` 轮廓——分析报告原话是"需要 OCR/形状识别"）

```
这是一张机械工程图的局部放大图，来自 <图纸名>。
图中有一组数值标注，我需要你读出它们的确切数字。

请只回答你确实看清的内容，看不清就说"看不清"，不要推测。

对每一个可见的数字标注，给出：
1. 数字本身
2. 它在图中的大概位置（左/中/右，上/中/下）
3. 你对该读数的把握程度（高/中/低）

不要解释工程含义，不要补充图中没有的内容。
```

**模板 B：裁决比例矛盾**（本流程实测比值离散 4.24–19.57）

```
这是一张工程图的整体视图，标题栏声明比例 1:15。

我实测了每个标注"数字 ÷ 图上画出的长度"，结果分为两组：
  一组约 14.88：676, 920, 525, 769
  一组约 17.86：984, 1536, 694.8, 624, 607.2, 504
  另有 30 约 4.24，432 约 5.57

问题：请你判断这四组数各自量的是图上的哪条线/哪两个面之间的距离。
具体来说，请指出：
1. 676 标注的两个尺寸界线分别对到哪个特征上
2. 432 标注的两个尺寸界线分别对到哪个特征上
3. 30 标注的两个尺寸界线分别对到哪个特征上

如果图上信息不足以判断，直接说"无法判断"，不要猜。
```

**模板 C：零件的语义识别**（对应"尚未建立完整实体映射"这个缺口）

```
这是一张工程图的局部放大图。图中包含若干个机械零件轮廓。

请识别你看到的每一类构件，给出：
1. 它是什么（例如：支承板 / 端盖 / 法兰 / 保温层 / 砖层）
2. 你依据什么判断（形状特征、剖面线方向、颜色）
3. 把握程度（高/中/低）

只识别你确实看清楚的。不要推测未显示的结构。
```

### 4.3 拿到回答之后

把视觉模型的回答**当作待验证的假设**，不是答案：

1. 它说的数字/归属，回到 `extract.json` 里核对几何是否支持
2. 若几何支持 → 写进 `cad-reproduce.yaml` 或作业配置
3. 若几何矛盾 → 记录下来问用户

---

## 5. 交付物

```
$JOB/
├─ source.pdf                  源图（权威副本）
├─ extract.json                抽取结果
├─ dims.json                   标注推断结果
├─ repro.dxf                   成品（可编辑）
├─ repro.dwg                   DWG（若跑了 --dwg）
├─ build.json / cmp.json       构建与自检报告
├─ trace/                      同网格渲染对
├─ out/cmp_difference.png      差异图 ← 给人看的关键图
└─ acad/repro_export.pdf       AutoCAD 出图（权威）
```

---

## 6. 卡住了先看

- `references/PITFALLS.md` —— **44 条实测坑**，按 AutoCAD 无头调用 / ezdxf / 几何语义 /
  验证尺子 / 环境 / **门禁看不见的缺陷** 分类。九成问题在这里有答案。
  第 38–44 条是"流水线不报错、成品是错的、门禁还给绿灯"的那一类，换图纸时最值得先读。
- `references/PIPELINE.md` —— 每步完整调用、判据、**门禁的能力边界**、
  以及按症状查的排错对照表。
- `evals/test_repository.py` —— **先跑这个**：仓库自身的一致性（文档链接、Skill 可加载、
  文档承诺与代码接线是否一致），并把下面两套行为测试一起跑掉。
- `evals/test_regressions.py` —— 四类历史缺陷的回归测试，改动脚本后必跑。
- `evals/test_arc_recognition.py` —— 圆弧/圆识别（PDF 路径里区分真弧与曲线拟合）的回归测试。
- **"为什么是这样而不是那样"** 不单独成文，而是写在对应代码与上面的坑清单里：
  每条坑都带**当时量到的数字**（例如"箭头写成 2.5pt 而源图是 5.4pt"），
  那就是这个设计决策的理由。判断某个参数能不能改之前，先搜 `PITFALLS.md` 里
  有没有它的实测记录。
