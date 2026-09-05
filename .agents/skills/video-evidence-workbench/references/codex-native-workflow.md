# Codex 原生批次流程

`analyze-video` 保存证据与项目状态；当前 Codex 只检查本批绑定的图像、音频或文本证据并提出分析。调用者先选择已有项目或明确授权新的提取动作。

```sh
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects codex next PROJECT --batch-size 12
# 查看 next 返回的准确帧和音频证据，填写 response_template：
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects codex submit PROJECT --result /absolute/batch-response.json
python3 -I "$SKILL_ROOT/scripts/workbench.py" --workspace ./analysis-projects codex status PROJECT
```

`next` 只给尚未完成的镜头和必要邻接上下文。每一批都要检查指定帧；快速动作、遮挡、转场、机位关系或连续运动不明确时，再查看源片段或补充帧。不要从文件名猜测未记录的抽帧时刻，也不要把联系表当成逐帧证据。

原片无音轨时，工作台会绑定 `audio_wav.status=absent`。在交付里明确「原片无音轨」，不要添加静音轨，也不要把无音轨写成已经听到的静默。没有可检查的声音证据时，声音判断应标为未知。

每次 `submit` 只能提交当前请求的有效子集，受 1–32 镜和 1 MiB 边界约束。中断时再次运行 `next`；已保存的相同行会复用。全量完成后工具按当前证据重新校验并应用。证据漂移、schema 错误或冲突不会覆盖已有镜头，应重新准备当前证据，不能强行写入。

模型提交是待复核提案。它不能修改来源、时间码、帧路径、审阅状态或人审声明；也不能替用户 Finalize、导出或声称用户看过证据。只有用户明确要求才继续 Finalize 或导出，随后分别回读其真实结果。
