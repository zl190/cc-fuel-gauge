# Context Degradation Ate My YAML

*Building a context monitor that got degraded by its own context*

---

Three out of three. That was my YAML validation rate on the short test transcript. Perfect score. I shipped the prompt, ran it against a real session transcript, and got three out of three failures. The model wasn't generating YAML. It was hallucinating a fake conversation between a user and assistant about running A/B tests. It was completing the transcript instead of following my instructions.

I was building cc-fuel-gauge, a Claude Code statusline plugin that warns you when your context window is degrading. Part of the pipeline: when context hits a threshold, auto-generate a structured handoff document so the next session can pick up where you left off. The handoff is YAML. The generator is Claude Haiku, chosen because it's cheap and fast enough to run inline.

I wrote an A/B test harness to validate the YAML output. First test: a 393-token transcript. Clean. Valid YAML, all five schema fields present, three for three. I almost moved on.

Then I tested with a real session transcript. 6.4K tokens from an actual cc-fuel-gauge development session. Every single output was garbage. Not malformed YAML -- no YAML at all. the system prompt had no observable effect on Haiku's output -- it treated the transcript as context to continue, generating a plausible-looking but completely fabricated conversation about running A/B tests. Given the overwhelming volume of conversational context, "continue the conversation" was simply the highest-probability generation path.

## The Prompt Structure That Failed

Here's what the prompt looked like:

```yaml
# System prompt
role: system
content: |
  You are a handoff document generator. Generate valid YAML
  matching this schema:
  - session_id: string
  - key_decisions: list
  - open_questions: list
  - file_changes: list
  - next_steps: list

# User message
role: user
content: |
  <transcript>
  ... 6.4K tokens of real session ...
  </transcript>
  Generate handoff.yaml from this transcript.
```

At 393 tokens, this worked. At 6.4K tokens, it broke completely. Same prompt template. Same model. Same temperature. Only the input length changed.

## Three Mechanisms, One Diagnosis

I'd been reading the context degradation literature for cc-fuel-gauge's detection algorithm, and the failure mapped cleanly onto two of the three mechanisms I was tracking.

**Mechanism I: Softmax attention dilution.** Under our proposed framework, this would scale with absolute token count. Softmax normalizes attention weights across all tokens. More tokens increases the entropy of the attention distribution (Θ(log n)), meaning attention weight on any fixed token tends to decrease. At 393 tokens, the system prompt's schema definition commanded enough attention weight to steer generation. At 6.4K tokens, that weight diluted by roughly 16x. The schema became background noise. Nakanishi (2025), Vasylenko (2025), and Li (2025) provide evidence for this entropy-scaling behavior.

**Mechanism II: Lost-in-the-middle.** This scales with the ratio of where information sits relative to the full context. Liu et al. (2023) demonstrated a U-shaped attention curve: models attend best to the beginning and end of context, worst to the middle. My schema sat at the beginning (system prompt). My transcript filled the middle AND pushed the schema further from the instruction at the end. The schema fell into the attention trough.

At 393 tokens there was barely any "middle" to get lost in. At 6.4K tokens, the schema was separated from the generation instruction by thousands of tokens of transcript. Classic U-curve failure.

**Mechanism III: Positional encoding out-of-distribution.** Not relevant here. This kicks in when you approach or exceed the model's training-time context length. We were at 7.2K total tokens. That is well within Haiku's supported context range. (Du et al. (2025) provide separate evidence for positional distance effects: their attention-masking experiment showed degradation even when distractor tokens were excluded from softmax computation, which means the degradation cannot be softmax dilution -- it must arise from positional distance itself. That result would be Mechanism III territory, not Mechanism I.)

Under this hypothesis, two mechanisms may have compounded. Dilution weakened the schema's signal. Lost-in-the-middle buried what was left.

### Alternative explanations

The diagnosis above is my working hypothesis, but I should be honest: multiple alternative explanations predict the same fix would work. A reviewer rightly pointed out that "the fix worked" doesn't prove the mechanism -- that's affirming the consequent.

**Instruction-hierarchy artifact.** Haiku may have learned during training that when a long conversational transcript is present, continuation is the most likely desired behavior. The system prompt would be weak not because of attention mechanics but because Haiku's instruction hierarchy doesn't strongly override in-context conversational signals.

**Completion-mode switching.** At 6.4K tokens of conversational content vs ~200 tokens of instruction, the ratio tips the model into completion mode. This would be about content-to-instruction ratio, not absolute token count or positional effects.

**Prompt format sensitivity.** Some models handle structured content differently in system prompts vs user messages. The fix may work because of prompt formatting conventions rather than attention dynamics.

The LiM explanation is my working hypothesis because (a) it makes a testable prediction -- moving the schema should help -- which it did, and (b) the three-mechanism framework gives a systematic way to diagnose similar failures. But I haven't run the tests that would rule out alternatives. A proper test would hold position constant and vary length, or hold length constant and vary position. I did neither.

## The Fix: 10 Lines

The theory pointed directly at the solution. If lost-in-the-middle penalizes the schema's position, move the schema. If recency bias is the strongest signal, put the schema at the end.

Before:

```
[SYSTEM: schema + instructions]
[USER: transcript + "generate yaml"]
```

After:

```
[SYSTEM: "You are a YAML generator."]
[USER: transcript + schema + "generate yaml"]
```

The schema moved from the system prompt to the end of the user message, right before the generation instruction. The transcript became the thing that's lost in the middle, which is fine -- the model only needs to extract information from it, not follow it as an instruction.

Ten lines changed in the prompt template. That's it.

## The Numbers

**Prompt fix (schema placement):**

| Run | Prompt v1 (schema in system) | Prompt v2 (schema at end) |
|-----|-----|-----|
| 1 | INVALID (hallucinated conversation) | Valid, 5/5 schema fields |
| 2 | INVALID (hallucinated conversation) | Valid, 5/5 schema fields |
| 3 | INVALID (hallucinated conversation) | Valid, 5/5 schema fields (validator bug masked it) |

Zero out of three to three out of three. The fix was pure information placement -- same words, different position.

Then I ran a feasibility check: could a local model handle this task? I tested Qwen3.5-4B (4-bit GGUF on M1 Pro) and Claude Haiku API, both using the fixed prompt, on a real 10K-token transcript.

**Local vs API:**

| Metric | Qwen3.5-4B (local) | Claude Haiku (API) |
|--------|----|----|
| Valid YAML | **3/3** | 1/3 |
| Schema 5/5 | **3/3** | 1/3 |
| Epistemic tags | **3/3** | 1/3 |
| Avg latency | 77s | **19s** |
| Cost per call | **$0** | $0.02 |

**Important caveat:** Qwen3.5-4B and Claude Haiku differ in architecture, tokenizer, parameter count, quantization, and runtime. This is too many confounded variables for a meaningful head-to-head comparison. The takeaway is feasibility -- a 4B local model CAN produce valid handoff YAML for this task -- not that one model is superior to the other.

That said, the local model produced valid YAML on all three runs at this input length, while the API model had parse errors on two of three. For this specific use case, the local option was viable.

77 seconds sounds slow until you remember this runs in the background when context hits a threshold. Nobody's waiting for it.

## The Meta-Irony

I need to say it plainly: I was building a tool to detect context degradation, and context degradation broke my tool. The handoff generator that's supposed to fire when context quality drops was itself a victim of the same phenomenon it was designed to warn about.

The fix aligned with the framework I was investigating. The three-mechanism decomposition -- softmax dilution, lost-in-the-middle, positional encoding OOD -- isn't just a taxonomy for a detection algorithm. It's a debugging framework. When my YAML generation failed, I didn't randomly try prompt variations. I diagnosed which mechanisms were active, predicted what would fix it, and tested that prediction. It worked on the first try.

## What I Learned About Information Placement

Context engineering is not prompt engineering. Prompt engineering is about what you say. Context engineering is about where you put it.

Four rules I now follow, based on this one experience and the literature I was reading at the time (your mileage may vary):

1. **Critical instructions go at the end.** Recency bias is the strongest positional signal in transformer attention. Your schema, your output format, your constraints -- put them as close to the generation point as possible.

2. **Sandwich variable content.** If you must put instructions at the beginning AND end, make the variable-length content (transcripts, documents, retrieved chunks) the filling. It can afford to lose attention. Your instructions can't.

3. **Absolute token count matters even within limits.** My 6.4K tokens were nowhere near Haiku's context window. Didn't matter. Every token you add dilutes attention on every other token. Trim ruthlessly.

4. **Test with realistic input lengths.** My 393-token test was useless. It told me the prompt template was correct. It told me nothing about whether the prompt would work at production input sizes. The failure mode only appeared at scale. If I'd only tested short inputs, I'd have shipped a broken pipeline.

The debugging pattern generalizes beyond YAML generation. Any time a prompt works on short inputs and fails on long ones, if this framework holds, check these three proposed mechanisms in order. Softmax dilution (is the context just too long?), lost-in-the-middle (is the critical information in the wrong position?), positional encoding OOD (are you past training-length context?). Usually it's the first two.

## References

- Du et al. (2025). "Context Length Alone Hurts LLM Performance." arXiv:2510.05381
- Veseli et al. (2025). "Positional Biases Shift as Inputs Approach Context Window Limits." arXiv:2508.07479
- Liu et al. (2023). "Lost in the Middle: How Language Models Use Long Contexts." arXiv:2307.03172

---

*Your prompt works on short inputs and fails on long ones? The model isn't broken -- your information placement is.*
