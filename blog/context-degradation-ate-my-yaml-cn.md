# Context 降质吃掉了我的 YAML

**做 context 监控工具的时候，被 context 降质反噬了**

---

我在做一个 Claude Code 的 statusline 插件，叫 cc-fuel-gauge。功能很简单：在终端状态栏显示当前 context window 的使用情况，但不是显示百分比——显示绝对 token 数。因为 1M 窗口的 20% 是 200K tokens，20K 窗口的 20% 是 4K tokens，这两个 "20%" 的降质程度天差地别。

插件还有个自动功能：context 到了危险阈值，自动调一个小模型（Haiku 或本地 Qwen3.5-4B）生成一份结构化的 handoff.yaml，把当前会话的状态压缩下来，下次新会话直接读这个文件接着干。

handoff 的 schema 不复杂，但字段不少：项目元数据、当前任务进度、决策记录（分 verified / proposed / rejected 三种标签）、文件变更、下一步计划。总之是一份完整的会话快照。

做好了，该测了。

## 短 transcript：一切正常

我先拿了个 393 tokens 的短 transcript 测。调 Haiku API 三次，三次都返回了合法 YAML，schema 字段齐全，validator 全过。

当时觉得：行，这功能基本没问题了。

## 真实 transcript：全军覆没

然后换成真实的会话 transcript——6.4K tokens，一个正常的 Claude Code session 导出。同样调 Haiku，同样的 prompt，跑三次。

三次全挂。

不是格式错误，不是字段缺失。Haiku 根本没生成 YAML。它幻觉了一段对话——用户和 AI 在讨论某个 A/B test 的设计方案，有来有回，煞有介事。它在**续写 transcript**，不是在**执行指令**。

System prompt 明确写了 "You are a YAML generator. You output ONLY valid YAML. No markdown fences. No explanation. No conversation. Just YAML." 白写了。6.4K tokens 的 transcript 塞进去之后，这条指令就像没存在过一样。

## 诊断：三个机制

我正好在写一篇关于 context window 降质的论文，提出了一个三机制框架。这次的 bug 完美地撞上了其中两个。

**机制一：Softmax attention dilution。** 自注意力的 softmax 分布，随着 token 数增长，entropy 以 Theta(log n) 的速度增加。通俗说：token 越多，注意力越分散。6.4K tokens 不算多，但对 Haiku 来说，已经够让 system prompt 里的关键指令被稀释了。Du et al. (2025) 做过一个实验：光是用 attention mask 填充 padding tokens（不含任何实际信息），就能让 HumanEval 成绩掉 50%。绝对 token 数本身就有毒。

**机制二：Lost-in-the-Middle。** Liu et al. (2023) 发现的经典效应。模型对 context 开头和结尾的信息注意力最强，中间的信息最容易丢。我的 prompt 结构是这样的：

```
[SYSTEM: schema + 指令]  ←── 开头
[USER: 6.4K tokens 的 transcript ... 生成指令]  ←── 中间到末尾
```

Schema 在 system prompt 里，transcript 在 user message 中间。经典的 U 型曲线中间低谷——schema 正好落在注意力最弱的位置。Veseli et al. (2025) 进一步证明了这个 U 型曲线的形状随 context 填充比例缩放，不是固定的。

两个机制叠加，Haiku 对 "你是 YAML 生成器" 这条指令的感知能力大幅下降。transcript 里充斥着对话内容，对模型来说，"续写对话" 是一个比 "执行 schema 生成" 更强的先验。于是它跟着 transcript 的惯性走了。

### 替代解释

上面的诊断是我的工作假设，但需要诚实地说：多个替代解释都能预测同样的修复会生效。一位审稿人正确地指出，"修复有效" 不能证明机制——这是肯定后件的谬误。

**指令层级产物。** Haiku 可能在训练中学到了：当存在一段长对话 transcript 时，续写是最可能的期望行为。System prompt 之所以弱，不是因为注意力机制，而是因为 Haiku 的指令层级在面对上下文中的对话信号时没有强力覆盖。

**补全模式切换。** 6.4K tokens 的对话内容 vs ~200 tokens 的指令，这个比例可能让模型切换到了补全模式。这关乎内容与指令的比例，而非绝对 token 数或位置效应。

**Prompt 格式敏感性。** 有些模型对 system prompt 和 user message 中的结构化内容处理方式不同。修复可能是因为 prompt 格式惯例，而非注意力动力学。

LiM 解释之所以是我的工作假设，因为 (a) 它给出了可测试的预测——移动 schema 位置应该有帮助——确实有效了，(b) 三机制框架提供了系统性诊断类似故障的方法。但我没有跑能排除替代解释的实验。严格来说需要控制位置不变而改变长度，或控制长度不变而改变位置。两个我都没做。

## 修复：10 行代码

修复方法直接来自机制二的推论。Lost-in-the-Middle 的反面是 recency bias——模型对末尾信息的注意力最强。那就把 schema 从开头挪到末尾。

Before:
```
[SYSTEM: 完整 schema + 严格指令]
[USER: transcript + "生成 handoff.yaml"]
```

After:
```
[SYSTEM: "You are a YAML generator. Output ONLY valid YAML."]
[USER: transcript + 完整 schema + "按这个 schema 生成，从 version: 2 开始"]
```

改动就是把 schema 从 system prompt 移到 user message 的末尾，紧贴最后的生成指令。System prompt 只保留一句话的角色定义。

实际代码改动大概 10 行。

结果：0/3 有效 -> 2/3 有效。第三个 run 其实也生成了合法 YAML，只是 validator 有个 bug 误报了。等于实际 3/3。

## 数据

**Prompt 修复效果（schema 位置）：**

| | Prompt v1（schema 在 system prompt） | Prompt v2（schema 在末尾） |
|---|---|---|
| Run 1 | 无效——幻觉了一段 A/B test 对话 | 有效，schema 5/5 字段齐全 |
| Run 2 | 无效——续写 transcript | 有效，schema 5/5 字段齐全 |
| Run 3 | 无效——续写 transcript | 有效（validator bug 掩盖了结果） |

同样的 prompt 措辞，只是换了位置。0/3 变 3/3。

修完 prompt 之后，我做了一个可行性验证：本地模型能不能完成这个任务？测试了 Qwen3.5-4B（4-bit GGUF，M1 Pro 上用 llama-cpp-python 加 Metal 加速）和 Claude Haiku API，用的是当前 session 的真实 transcript（10K tokens）。

**本地 vs API：**

**重要说明：** Qwen3.5-4B 和 Claude Haiku 在架构、tokenizer、参数量、量化方式和运行环境上都不同。混杂变量太多，无法进行有意义的直接对比。这里的结论是**可行性验证**——4B 本地模型能为这个任务生成合法的 handoff YAML——而不是哪个模型更优。

| 指标 | Qwen3.5-4B（本地） | Claude Haiku（API） |
|------|----|----|
| YAML 有效 | **3/3** | 1/3 |
| Schema 齐全 | **3/3** | 1/3 |
| Epistemic 标签 | **3/3** | 1/3 |
| 平均延迟 | 77s | **19s** |
| 单次成本 | **$0** | $0.02 |

在这个输入长度下，本地模型三次都生成了合法 YAML，API 模型有两次解析错误。对于这个特定用例，本地方案是可行的。

77 秒听起来慢，但这个功能是 context 到阈值时后台自动触发的。没人在等它。

## Meta-讽刺

我做的工具是用来监控 context 降质的。这个工具的核心功能——自动生成 handoff 文档——被 context 降质搞崩了。修复它的理论依据，来自我正在写的那篇论文。

论文的核心论点是：context degradation 不是一个单一现象，而是三个不同机制的叠加，每个机制有不同的 scaling variable。这个 bug 是前两个机制在真实场景下的活体演示。

如果我没在写这篇论文，大概率会在 prompt 措辞上反复调整——加粗、重复、换说法。但因为脑子里有这个框架，直接定位到了问题的物理层面：不是措辞不够强，是信息放错了位置。

## 教训

**Context engineering 不是 prompt engineering。** Prompt engineering 关注的是怎么说。Context engineering 关注的是**在哪说**。同样的指令，放在 system prompt 开头还是 user message 末尾，在短 context 下没有区别，在长 context 下是生与死的区别。

**关键指令放末尾。** 利用 recency bias 而不是对抗它。Schema、约束、输出格式——这些东西应该紧贴最后的生成指令，不要放在开头期望模型一直记得。

**用真实长度输入测试。** 393 tokens 和 6.4K tokens 的差距不是线性的。短输入掩盖了所有 attention dilution 和 positional bias 的问题。你的 unit test 全过了不代表 production 没问题。

**绝对 token 数才是关键，不是百分比。** 6.4K tokens 在 200K 的窗口里只占 3%，看起来毫无压力。但绝对数量已经够让模型丢失 system prompt 的约束了。别看百分比，看绝对值。

---

*References:*
- Du et al. (2025). "Context Length Alone Hurts LLM Performance." arXiv:2510.05381
- Liu et al. (2023). "Lost in the Middle: How Language Models Use Long Contexts." arXiv:2307.03172
- Veseli et al. (2025). "Positional Biases Shift with Context Length." arXiv:2508.07479

*cc-fuel-gauge: github.com/your-username/cc-fuel-gauge*
