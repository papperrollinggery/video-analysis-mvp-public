---
name: video-evidence-workbench
description: "在 Codex 中分析本地视频、广告或参考片，完成可追溯的镜头与声音拆解、分批续做和复核导出。可作为独立全局 Skill 使用，保留原片、分帧和人审证据边界。"
metadata:
  version: "0.3.0"
---

# Video Evidence Workbench

把用户的视频问题落实为有时间码、有图像或声音依据、能继续使用的分析。Workbench 提取和保存证据，当前 Codex 负责看、听、解释和交付。无需另配视觉 API，也不需要另开一套分析工程。

## 先确定要完成的工作

从用户请求和现有项目中确定视频、分析范围、关注问题及交付物。普通选择直接判断；只有用户独有的信息会改变结果时才问。

- **定位问题 / 看懂片段**：回答指定问题，给准确时间范围和必要证据；不顺手生成全套文档。
- **完整拆片**：覆盖约定全片的动作、镜头关系、表演与声音；逐镜内容写差异，共用风格或限制只写一次。
- **提炼机制 / 复拍准备**：先完成相应证据，再给「场景问题 → 观察 → 机制 → 可执行控制 → 适用与误用边界 → 检查点」。已有 `film-breakdown-distiller` 时只加载本次需要的参考；未安装也能按此结构完成当次成果。衍生创作按用户范围推进。
- **交付 Excel / PDF**：使用现有 review → Finalize → export。分析完成、人审完成、导出成功分别回读，不以一个状态替代另一个。

## 执行入口

全局安装版通过本 Skill 的 `scripts/workbench.py` 调用与它绑定的隔离运行时；它不依赖调用项目的 PATH、配置或虚拟环境。开发仓库中的同一文件会回退到本仓库 `.venv/bin/analyze-video`，仍保持调用者当前目录。

从当前 Skill 根目录执行下列 wrapper（`$SKILL_ROOT` 指向包含本文件的目录）：

```sh
python3 -I "$SKILL_ROOT/scripts/workbench.py" --runtime-info
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects doctor
```

如果 wrapper 报告没有绑定运行时，先由操作者以明确本地 wheel 安装：

```sh
python3 -I "$SKILL_ROOT/scripts/install.py" --wheel /absolute/video_analysis_mvp-0.3.0-py3-none-any.whl --extras api --extras export --extras pdf
```

安装器不下载 wheel、不读取凭据、不改全局 PATH 或其他 Skill。默认只装基础能力；需要 API、Excel 或 PDF 功能时显式重复传入 `--extras api`、`--extras export`、`--extras pdf`。它创建版本、wheel 摘要和 extras 绑定的隔离 venv，并以隔离解释器验证实际模块和发行版本，再原子切换本 Skill；旧同名 Skill 备份在 Skill 发现目录之外，可供人工回退。需要在测试或隔离环境指定位置时可用 `--skills-dir` 与 `--runtime-home`。

先确认现有 workspace/project，优先复用相符的源文件与有效提取结果。仅环境缺失或证据无效时运行 `doctor`，不要每批重复检查或重跑提取。

首次处理本地文件的例子（路径、profile 和项目 ID 按当前任务填写）：

```sh
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects run /absolute/video.mp4 --project-id example --profile research --delivery-language zh --skip-asr
```

`--skip-asr` 只适用于不需要转录或本地 ASR 不可用的情况；声音结论仍需真实听取。需要转录时使用已配置的本地模型，未提供模型不自动下载。完整分析超过 60 秒的视频时，按 ffprobe 实测时长设置 `--max-duration-seconds`，无需对已授权的整片分析重复确认。新建项目使用新 ID；旧项目提取失效时先诊断，只重做必要阶段。

原片没有音轨时直接走无音轨分支，交付中注明「原片无音轨」。不能给原片加人工静音音轨来绕过提取失败，也不能把没有音轨写成已测得静默。流程若失败，先定位当前阶段并修复或报告实际阻塞，不能把替换过的媒体称为原片测试通过。

```sh
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects codex next example --batch-size 12
# 查看该批证据，填写返回的 response_template，再提交：
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects codex submit example --result /absolute/batch-response.json
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects codex status example
```

重复 `next → 检查 → submit` 直到返回 `applied`。`next` 只返回未完成镜头及相邻上下文；中断后继续同一项目。每批都要实际看图，不能用脚本把同一句话复制到全部字段。空模板不能提交。一次请求最多 1024 镜，每批最多 32 镜；超过范围先明确分段交付，不能静默取前段。

初次调用或出现冲突时，读取 [Codex 原生流程](references/codex-native-workflow.md) 的相应部分，了解批次、纠正和恢复命令。小任务也可使用原有 `prepare → apply`。再次 `prepare` 默认复用当前有效分析；只有明确要重分析时使用 `--refresh`。

## 看什么，写什么

读取批次中的准确文件。起中末帧用于对比；接触表用于定位。快速动作、遮挡、转场或不明确的运镜需要查看源片段或补充关键时点。现有帧没有记录精确抽样时刻时，不从文件名编造时间。证据不足就写清未知，不能凭单帧断言连续推拉摇移。

完整拆片要检查：行动的准备、发生、结果是否漏掉；前后镜的信息增量、视线与屏幕方向是否连贯；是否需要保留听者反应；声音的出现、停顿、转折能否从实际音轨定位。机器分段数量不是完整分镜数量。模型无法听音频时，分别交付已完成的视觉分析与未验证的声音范围。

响应只填当前 schema 的字段，写具体可见事实。动作、构图、内容概述各写各的用途；适用的共享描述不用扩写成每镜小作文。观察、解释和生成建议在文字中说清。`quality` 里的重复字段与单帧提示是复核线索，不能当作语义评分或质量合格证。

会改变分析结论或复拍控制的关键主张，要能回指本次请求中的具体镜头或帧。有歧义时重新看该绑定帧，纠正文字并调整信心；不要把旧项目、未采用版本或相邻帧的印象写成当前证据的事实。

不要直接改 `shots.json`、时间码、来源回执或人审字段。旧记录即使写着 `human`，若内容是 Codex 自称人工审核，也不把它作为真实人审证据。保留旧件；需要重做时在新的分析项目中回看源片。

## 完成与交付

把分批结果合成用户要的答案或文件，说明依据及实际未验证处。检查全片首尾和关键转折都被覆盖、没有用重复套话掩盖缺项。提炼机制只保留有明确用途和检查方法的内容；需要持久化机制库时遵循用户授权。

回读 `applied` 与相关产物；模型提交仍是待复核提案。用户已要求的 Finalize/导出继续执行，不重新索要同一授权；不能替用户声明已看过证据。报告真实文件路径、测试或回读结果及必要限制。完成所请求的工作后停止，不自动做衍生视频、发布或跨项目改动。
