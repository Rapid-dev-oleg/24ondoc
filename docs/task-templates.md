# Шаблоны задач

Используйте эти шаблоны при создании новых задач в Paperclip. Каждый шаблон содержит обязательные поля и критерии приёмки, чтобы исключить неясности и переоткрытия.

---

## 1. QA Task Template

```markdown
## QA Task Template

**What to test:** [feature/flow/integration]
**Where to test:** [production/staging/local]
**Expected result:** [what should appear where]

**Acceptance criteria:**
- [ ] Criterion 1 (measurable, verifiable)
- [ ] Criterion 2
- [ ] Evidence provided: [screenshot/log/link to CRM entity]

**Definition of Done:**
Task is done when board can see [X] in [System Y] without assistance.
```

---

## 2. Development Task Template

```markdown
## Development Task Template

**Feature/Component:** [what to build]

**Acceptance criteria:**
- [ ] Criterion 1
- [ ] Criterion 2

**Code quality requirements:**
- Minimum coverage: [X%]
- Linters must pass: [yes/no, which ones: ruff/mypy/black/etc.]
- Tests must pass: [yes/no]

**Deployment required:** [yes/no]
- If yes: deployment target (VPS/staging/production)
- If yes: deployment verification steps

**Definition of Done:**
Task is done when board can [do X] and all code quality gates pass.
```

---

## 3. Bug Fix Task Template

```markdown
## Bug Fix Task Template

**Bug description:** [what's broken]

**Reproduction steps:**
1. Step 1
2. Step 2
3. Observe: [actual behavior]

**Expected behavior:** [what should happen]

**Acceptance criteria:**
- [ ] Bug reproduced in test
- [ ] Fix implemented
- [ ] Test passes after fix
- [ ] Smoke test of entire flow passes
- [ ] Deployed (if required)

**Definition of Done:**
Task is done when board cannot reproduce the bug and all related flows work correctly.
```

---

## 4. Infrastructure Setup Task Template

```markdown
## Infrastructure Setup Task Template

**Infrastructure component:** [VPS/DB/CRM/etc.]

**Required credentials/tokens:**
- [ ] Credential 1 (format: [X], how to provide: [Y])
- [ ] Credential 2

**Setup steps:**
1. Step 1
2. Step 2

**Verification checklist:**
- [ ] Board can access [System] at [URL]
- [ ] Board can perform [Action]
- [ ] Logs show [Expected state]

**Definition of Done:**
Task is done when board can use [System] without assistance and verification checklist is complete.
```
