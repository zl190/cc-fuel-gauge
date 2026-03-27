# cc-fuel-gauge

**Know when your context is actually degrading, not just filling up.**

[![License](https://img.shields.io/github/license/zl190/cc-fuel-gauge?style=flat-square)](LICENSE)

## The Problem

Every context window indicator you have seen shows a percentage. 20% used, 50% used, time to compact. But degradation does not scale with percentage — it scales with **absolute token count**.

20% of a 1M-token window is 200K tokens. 20% of a 20K window is 4K tokens. These are not remotely equivalent. Your "20% used" status bar is lying to you.

cc-fuel-gauge is a Claude Code statusline plugin that uses empirically-informed absolute thresholds to tell you when context quality is likely dropping, and optionally triggers a handoff before it gets bad.

## Why Absolute Thresholds

Three mechanisms drive context degradation, and they scale differently:

| Mechanism | Scales with | Effect |
|-----------|------------|--------|
| **Softmax attention dilution** | Absolute token count (entropy ~ O(log n)) | Attention spreads thin as n grows, regardless of window size |
| **Lost-in-the-Middle positional bias** | Ratio n/W (U-curve) | Middle tokens get buried; shape depends on fill ratio |
| **Positional encoding OOD** | Absolute position vs training exposure | Positions the model never trained on become unreliable |

The first and third mechanisms dominate in practice. Our working hypothesis is that a 200K-token conversation degrades similarly whether the window is 200K or 1M — the model has the same number of tokens competing for attention. This is the premise cc-fuel-gauge is built on, but it has not been rigorously tested across models and tasks.

For details, see "Three Mechanisms, One Degradation" (2026, link TBD).

**Key references:**
- Du et al. (2025). "Context Length Alone Hurts LLM Performance." [arXiv:2510.05381](https://arxiv.org/abs/2510.05381)
- Hsieh et al. (2024). "RULER: What's the Real Context Size of Your Long-Context Language Models?" [arXiv:2404.06654](https://arxiv.org/abs/2404.06654)
- Veseli et al. (2025). "Positional Biases Shift." [arXiv:2508.07479](https://arxiv.org/abs/2508.07479)
- Chroma (2025). "Context Rot." [trychroma.com/research/context-rot](https://trychroma.com/research/context-rot)

## Quick Start

```bash
git clone https://github.com/zl190/cc-fuel-gauge.git
cd cc-fuel-gauge
./install.sh
```

The installer adds the statusline hook to your Claude Code configuration. Restart your session to see the gauge.

## Default Thresholds

Thresholds auto-scale based on the model's context window size. Models trained on larger windows (e.g., Opus 4.6 at 1M) have stronger long-context training and degrade more slowly than 128K models.

| Window Size | Green | Yellow | Red | Evidence |
|-------------|-------|--------|-----|----------|
| ≤ 200K | < 30K | 30K–50K | > 50K | NoLiMa: 11/13 models < 50% at 32K |
| 200K–500K | < 50K | 50K–100K | > 100K | Interpolated |
| 1M+ | < 80K | 80K–200K | > 200K | MRCR: Opus 4.6 at 93% at 256K; Chroma: 30-60% gap on complex tasks |

You can override auto-scaling by setting explicit thresholds in your config file.

These are empirically-informed starting points, not sharp cliffs. Degradation is continuous and task-dependent -- a coding task may tolerate more context than a complex reasoning task. Tune to your workflow.

## Configuration

cc-fuel-gauge reads from `~/.config/cc-fuel-gauge/config.yaml`. If the file does not exist, defaults from `lib/defaults.sh` are used.

```yaml
# Threshold mode:
#   absolute — color based on absolute token count (default, recommended)
#   ratio    — color based on percentage of context window used
#   auto     — absolute thresholds + additional warning at high ratios
mode: absolute

# Absolute thresholds (tokens) — auto-scaled by window size if omitted
# soft_threshold: 30000    # yellow zone (uncomment to override)
# hard_threshold: 50000    # red zone (uncomment to override)

# Ratio thresholds (percentage, used when mode=ratio or mode=auto)
ratio_soft: 50
ratio_hard: 75
auto_ratio_warn: 80      # extra warning in auto mode

# Display
show_tokens: true        # show (50K/1M) alongside the bar
show_cost: true          # show session cost
compact: false           # compact mode: bar + tokens only

# Handoff (optional)
handoff:
  enabled: false
  method: local          # "local" (LM Studio) or "api" (Claude Haiku)
  model: qwen3.5-4b      # local model name
  api_model: claude-haiku-4-5-20251001
```

### Mode Comparison

| Mode | Behavior | Best for |
|------|----------|----------|
| `absolute` | Colors based on token count only | Most users. Empirically-informed default. |
| `ratio` | Colors based on percentage of window used | Users who prefer the traditional model. |
| `auto` | Absolute thresholds, plus a warning when ratio exceeds `auto_ratio_warn` | Users who want both signals. |

## Auto-Handoff

When enabled, cc-fuel-gauge can trigger an automatic context handoff when you cross the red threshold. The pipeline:

```
statusline.sh
  └─ detects threshold breach
      └─ handoff-trigger.sh
          ├─ local-handoff.py   (LM Studio + Qwen3.5-4B)
          │   └─ generates handoff.yaml
          └─ api-handoff.sh     (Claude Haiku API)
              └─ generates handoff.yaml

New session reads handoff.yaml → resumes with compressed context
```

**Local mode** runs Qwen3.5-4B through LM Studio on your machine. No tokens leave your network. This is the recommended setup if you have the hardware (4GB+ VRAM).

**API mode** calls Claude Haiku to generate the handoff summary. Faster, no local GPU needed, but sends context to the API.

To enable:

```yaml
handoff:
  enabled: true
  method: local    # or "api"
```

## Requirements

- **Claude Code** (the statusline hooks into its session)
- **bash**, **jq** (standard on macOS/Linux)
- (Optional) **LM Studio** + **Qwen3.5-4B** for local handoff generation

## Roadmap

- [x] Absolute threshold statusline
- [x] YAML configuration
- [ ] Auto-handoff via local model
- [ ] Auto-handoff via API
- [ ] tmux statusline integration
- [ ] Token count history / trend display

## License

[MIT](LICENSE)

## Acknowledgments

Built on research from Du et al., Hsieh et al., Veseli et al., and the Chroma team. The core insight — that absolute position matters more than relative fill — came from observing real degradation patterns in long Claude Code sessions.
