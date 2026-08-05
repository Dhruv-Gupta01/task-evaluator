# Cost comparison: Fireworks AI vs. RunPod self-hosting (gpt-oss-120b)

**Status:** Fireworks numbers are real (measured). RunPod numbers are estimates pending an
actual measured run — see [Open items](#open-items).

**Model:** `gpt-oss-120b` (OpenAI open-weight, MoE, 117B total / ~5.1B active params).
Runs on Fireworks as `accounts/fireworks/models/gpt-oss-120b`; self-hosted via vLLM as
`openai/gpt-oss-120b`.

---

## TL;DR

For our actual usage pattern — periodic 5-trial calibration batches, not sustained
continuous traffic — Fireworks is **roughly 40–100x cheaper** than any viable RunPod GPU
tier. The gap isn't really about which GPU you pick; it's a structural mismatch between
per-token billing and per-wall-clock-hour billing. See [Why the gap exists](#why-the-gap-exists).

---

## 1. Why gpt-oss-120b (and not DeepSeek, Kimi, Qwen3, etc.)

This reflects the trade-offs weighed in our earlier model-selection discussion, not a
formal benchmark study — treat the qualitative claims about other models as directional,
not verified against current published numbers (this space moves fast and model releases
update frequently).

**Candidates considered** (as open-weight alternatives to a proprietary baseline like
GPT-4.1-mini):

| Model                         | Architecture | Approx. size              | Self-hosting reality                                                                                                                                                                                       |
| ----------------------------- | ------------ | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **gpt-oss-120b** (chosen)     | MoE          | 117B total / ~5.1B active | Explicitly designed by OpenAI to fit on a **single 80GB GPU**                                                                                                                                              |
| DeepSeek (V3 / R1)            | MoE          | ~671B total / ~37B active | Needs a multi-GPU cluster (8x80GB+) for full precision; much heavier ops footprint                                                                                                                         |
| Kimi K2 (Moonshot AI)         | MoE          | ~1T total                 | Even larger than DeepSeek; similarly heavy multi-GPU hosting requirements                                                                                                                                  |
| Qwen3 (32B dense variant)     | Dense        | 32B                       | Genuinely single-GPU friendly, lighter than gpt-oss-120b, but a materially smaller/weaker model — the concern raised at the time was whether it was an "undersell" relative to the reasoning bar we needed |
| Qwen3 (235B-A22B MoE variant) | MoE          | 235B total / ~22B active  | Stronger than the 32B variant, but back into multi-GPU hosting territory                                                                                                                                   |

**Why gpt-oss-120b won out:**

1. **Single-GPU deployability was the deciding constraint.** DeepSeek and Kimi K2 are both
   substantially larger MoE models that need multi-GPU clusters to self-host at full
   precision — that's a materially different (and more expensive, per the RunPod math in
   §3) hosting story than gpt-oss-120b's single-80GB-GPU design, which we verified directly
   this session (§3.1 below). Qwen3's 235B variant has the same problem in a smaller way.
2. **Qwen3-32B was the lighter alternative actually considered**, but raised a real
   concern about whether it was strong enough for our reasoning bar (gpt-4.1-mini-class
   task performance) rather than a genuine downgrade dressed up as a cost saving —
   this was flagged explicitly as an open question ("undersell or oversell") rather than
   resolved with a real benchmark at the time.
3. **Zero-friction integration.** gpt-oss-120b was already available on Fireworks — a
   provider we'd already integrated (same `OpenAIClient` + `base_url` pattern used for
   Groq) — so testing it cost nothing beyond adding one more provider branch, versus
   standing up new infrastructure to evaluate DeepSeek/Kimi properly.
4. **It won on real, measured results, not just availability.** Once tested for real on
   our actual calibration task (`reconcile-report-config-v4`), gpt-oss-120b scored 5/5 —
   matching or beating gpt-4.1-mini's real 3-4/5 — at roughly 1/7th gpt-4.1-mini's
   per-trial cost on Fireworks (§2).

---

## 2. Fireworks AI — real, measured costs

Reconstructed from actual trial transcripts on `reconcile-report-config-v4` (5 real agent
trials, gpt-oss-120b), using each trial's real tool-call sequence and the real byte sizes of
every file the agent read, simulated forward through the growing conversation context each
API call actually resends.

| Metric                                     | Value                                                         |
| ------------------------------------------ | ------------------------------------------------------------- |
| Pricing                                    | $0.15 / 1M input tokens · $0.60 / 1M output tokens (uncached) |
| Per-trial cost (avg of 5 real trials)      | **~$0.0046**                                                  |
| Per-trial cost (range across the 5 trials) | $0.0039 – $0.0060                                             |
| Per 5-trial submission                     | **~$0.023**                                                   |

For comparison, gpt-4.1-mini on the same task (real measured, same reconstruction method):
~$0.011–0.012/trial, ~$0.055–0.06 per 5-trial submission — gpt-oss-120b came in at roughly
1/7th the cost per trial on Fireworks, on top of also scoring higher (5/5 vs 3-4/5).

**Caveats on the Fireworks numbers:**

- Token counts are reconstructed via a ~4-chars/token heuristic, not the model's real BPE
  tokenizer — likely accurate to within a small margin, not exact.
- Assumes no prompt-caching discount is being applied (Fireworks offers a lower cached-input
  rate; if the client benefits from it, real cost is somewhat lower than shown here).

---

## 3. RunPod self-hosting — estimated costs

**Not yet measured.** No trial has actually been run against a RunPod-hosted endpoint. The
figures below are projections built from this session's own _Fireworks_ trial timing, scaled
onto RunPod's per-hour billing — they are informed estimates, not facts.

### 3.1 GPU tier requirement

gpt-oss-120b's full 117B parameters must be resident in VRAM simultaneously (MoE routing
picks different experts per token; the inactive experts can't be paged out). In OpenAI's
native MXFP4 (4-bit) quantization that's ~60–65GB of weights alone, before KV cache and
runtime overhead — so **80GB is the practical minimum GPU tier**. Anything below that
(48GB L40S, 32GB RTX 5090, 24GB cards, etc.) cannot hold the model at all and isn't a real
option, regardless of price.

### 3.2 Estimation method

```
effective $/hr  = GPU on-demand rate + disk cost (~$0.006/hr)
session length  = setup/model-load overhead (~5 min, assumes weights already on a
                  persistent volume) + 5 trials × ~5 min/trial average
                  (using this session's own observed Fireworks-trial wall-clock time
                  as a proxy for total session length, since most of that time is
                  docker/tool-call/verifier overhead, not pure token-generation speed)
est. cost       = effective $/hr × session length (hours)
```

This is the biggest source of uncertainty in the whole comparison — see
[Open items](#open-items).

### 3.3 Estimated cost by GPU tier

| GPU                  | VRAM  | $/hr      | Effective $/hr (+disk) | Est. $/trial | Est. $/5-trial submission | vs. Fireworks |
| -------------------- | ----- | --------- | ---------------------- | ------------ | ------------------------- | ------------- |
| RTX PRO 6000         | 96GB  | $1.99     | ~$2.00                 | ~$0.17       | ~$1.00                    | ~43x          |
| H100 PCIe            | 80GB  | $2.89     | ~$2.90                 | ~$0.24       | ~$1.45                    | ~63x          |
| H100 SXM             | 80GB  | $2.99     | ~$3.00                 | ~$0.25       | ~$1.50                    | ~65x          |
| H100 NVL             | 94GB  | $3.19     | ~$3.20                 | ~$0.27       | ~$1.60                    | ~70x          |
| H200 SXM             | 141GB | $4.39     | ~$4.40                 | ~$0.37       | ~$2.20                    | ~96x          |
| **Fireworks (real)** | —     | per-token | —                      | **$0.0046**  | **$0.023**                | —             |

RunPod pricing pulled directly from the RunPod console (community cloud, on-demand) —
verify live before committing, these shift.

**RTX PRO 6000 caveat:** it's the cheapest and roomiest option in the table, but it's
Blackwell-generation workstation silicon. OpenAI's reference MXFP4 kernels for gpt-oss were
validated on Hopper (H100/H200); Blackwell kernel support may or may not be equally
optimized as of this writing. Worst case it falls back to bf16 dequantization (~2x memory,
would not fit in 96GB) or runs at reduced throughput. Treat it as "try first, fall back to
H100 if it errors," not a confident default.

### 3.4 Costs RunPod bills that Fireworks never does

- **Model load time**: loading ~65GB of weights into VRAM on every vLLM (re)start — a few
  minutes, billed at the full GPU rate, even with weights already on a persistent volume.
- **Idle time inside/between trials**: docker container setup, tool-call round-trips,
  verifier/pytest execution — all of this shows up as wall-clock time on RunPod's bill.
  Fireworks only bills for the actual token-generation slices.
- **First-time weight download** (one-time, if not already on a persistent volume): ~65-70GB
  from Hugging Face, roughly 10-20 minutes, add ~$0.50-1 depending on GPU tier. Amortizes
  away after the first session if using a persistent Network Volume.
- **RunPod storage, separate from GPU-hours**: a Network Volume or Volume Disk sized for the
  model weights (100GB+ recommended) costs $0.07-0.10/GB/month on top of GPU time — this
  keeps accruing even while the pod is stopped (Volume Disk: $0.20/GB/mo stopped; Network
  Volume: flat $0.07/GB/mo regardless of pod state).

---

## 4. Why the gap exists

This isn't "Fireworks is cheap," it's "a dedicated RunPod GPU bills for idle time that
Fireworks never charges for." Fireworks serves many customers off the same hardware, so the
idle time between any one customer's tokens gets amortized across everyone. A RunPod pod is
100% your bill, whether the GPU is actively generating tokens or sitting idle waiting on a
docker container or a pytest run.

**Switching to a cheaper GPU tier doesn't close this gap** — it only saves the ~30-50%
difference between the cheapest and most expensive viable tier (RTX PRO 6000 → H200 SXM).
The ~60x structural gap comes from the billing model itself, not the specific hardware
price. Self-hosting only becomes cost-competitive at **high, sustained GPU utilization** —
running close to continuously busy for extended periods — not periodic calibration batches
like the ones this comparison is based on.

---

## 5. Open items

- **No real RunPod measurement exists yet.** Every number in §3 is a projection from
  Fireworks-observed timing, not a measured RunPod trial. Real local vLLM throughput on
  dedicated hardware could be meaningfully faster (no multi-tenant queueing) or slower (no
  serving-side batching/tuning we'd have to configure ourselves) than what's assumed here.
- **To replace the estimate with a real number**: deploy `openai/gpt-oss-120b` via vLLM on a
  RunPod pod, wire it into the Task Evaluator platform as a new `runpod` provider (same
  pattern as the existing `fireworks`/`groq` providers — reuse `OpenAIClient` with a
  different `base_url`), run an actual 5-trial batch, and record the real wall-clock time and
  cost. Update this document with the measured figures once available.
- RunPod's own on-demand pricing shifts over time — the table in §3.3 should be re-verified
  against the live RunPod console before using these numbers for a real budgeting decision.

---

_Last updated: 2026-07-31. Generated from analysis in the Task Evaluator project's
model-comparison work (Fireworks gpt-oss-120b trials on `reconcile-report-config-v4`)._
