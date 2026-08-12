# Security (App-Specific Addendums)

Addendum to the standardized `.claude/security.md` and to the global supply-chain
rule in `general.md`. Nothing here relaxes those rules generally — each item below
is a narrow, deliberate exception with its own guardrails.

---

## ⚠️ Deliberate exception: two PRC-origin generative-media models are permitted

**Do not "fix" this. It is intentional and was approved explicitly.**

The global rule says *"NEVER use dependencies from PRC-based organizations or
sanctioned entities."* That rule stands for everything except the two models named
below, which are admissible under **spec §2.2a — Acknowledged-risk exception**.

| Model | Origin | Why excepted |
|---|---|---|
| **Kolors** | Kuaishou (PRC) | Image generation; no close non-PRC equivalent in its niche |
| **Open-Sora** | HPC-AI Tech — Singapore parent, Beijing R&D via 北京潞晨科技, PRC state-fund backing | Video generation; treated as PRC-origin despite Singapore incorporation |

Every other PRC-origin model remains denied outright — Qwen, DeepSeek, GLM/ChatGLM,
Yi, Kimi, MiniMax, **CogVideoX** (Zhipu AI — the same organisation as GLM, so the
existing rule already catches it), Wan, HunyuanVideo.

### The guardrails are the exception — do not remove them

If you are changing model registration, fleet placement, or the model registry,
these constraints are load-bearing:

- **Never a default.** Not a default, not a dual-default alternative, not a seeded
  registry row. Off unless a human turns it on.
- **`Role.ADMIN` only** (`shared/auth/rbac.py`). A Resource Manager must not be able
  to accept this even for an org they legitimately manage. `Role.ADMIN` is cross-org
  by construction — `check_permission` short-circuits with "Admin has access to
  everything" before org scoping runs.
- **Deployment-scoped, not org-scoped.** Weights execute on shared fleet
  infrastructure; a backdoored model runs in a pod on the same cluster with that
  pod's network and storage reach, whichever org invoked it. An org-scoped approval
  would be one tenant accepting risk for every other tenant.
- **Generation roles ONLY.** These models MUST NOT be assignable to `security-audit`,
  `routing-classifier`, `embeddings`, `summarize`, or any other internal-function
  row. The registry must **reject**, not warn.
  *Why:* a poisoned generation model produces output the §8.3a guardrails still
  inspect; a poisoned **classifier** fails open silently and takes the safety layer
  down with it. This asymmetry is the entire basis for allowing the exception at all.
- **Per-model acceptance**, recorded with admin identity, timestamp, model, and the
  version of the risk text accepted, audited to `security_logs`. Accepting Kolors
  must not enable Open-Sora.
- **Output is not exempt** from the §8.3a per-modality guardrails.
- The deny-list enforcement point gains an explicit *acknowledged-exception* branch —
  an affirmative, logged code path. **Not** a hole in the deny-list, and **not** an
  `origin` field quietly set to something other than PRC.

### Open-Sora carries a separate, unresolved licence question

Its code `LICENSE` reportedly appends the **Tencent Hunyuan Community License**,
barring use in the **EU, UK and South Korea**. That is a *licence* constraint which
the origin exception does **not** cure, and it is unverified against the primary
`LICENSE` file. Confirm before enabling it in those markets.

---

## Deliberate exception: non-commercial model weights in the Free tier

Also intentional. See spec §2.3, third licence class.

`MusicGen` / `AudioCraft` (Meta) and `AudioLDM 2` (University of Surrey) ship under
**CC-BY-NC** / **CC-BY-NC-SA**. The standard forbids `CC-BY-NC`, and that stands
everywhere else — but no self-hostable music model with commercial terms currently
exists, so these are offered under a restricted class:

- **Free tier only** — hard-disabled in Professional and Enterprise.
- ⚠️ **This inverts the usual licence-gate direction.** Every other tier gate asks
  "does this tier *unlock* X?"; this one asks "does this tier *forbid* X?" It is a
  **deny**, not an allow. Do not implement it as a missing unlock.
- **Off by default**, requiring a per-model `Role.ADMIN` acknowledgement recorded
  with identity, timestamp, model, and licence identifier **and version**.
- Labelled **"non-commercial use only"** wherever selectable.
- Free tier is a **scale** limit (≤5 nodes, ≤3 models), *not* a commercial-status
  one — a funded startup on Free still breaches CC-BY-NC. The acknowledgement is
  what makes this the operator's affirmation rather than our violation.

`Moshi` (Kyutai, France) is **CC-BY 4.0** — commercial use permitted. It is a normal
default in every tier and carries **neither** the label nor the gate. Do not lump it
in with the two above.

Commercial music generation is **passthrough only** — Lyria, Suno, Udio, ElevenLabs.
(Google **Veo** is video, not music.)

---

## Known stale file

`docs/standards/MODEL_ROUTING_MATRIX.md` states *"All models are EU/North American
origin only (no Chinese models)"*, which §2.2a now qualifies. It is app-specific
content sitting in the template-owned `docs/standards/` tree — it does not exist in
the admin template repo — so it cannot be edited under the root `CLAUDE.md` rule.
Relocating it out of `docs/standards/` is the real fix and needs a human decision.

---

**Authoritative source for all of the above:**
`docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §2.2, §2.2a, §2.3, §8.3a, §16.
