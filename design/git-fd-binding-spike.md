# Git descriptor-binding feasibility spike

**Issue #61.** Evidence for the Issue #48 re-split. This is a finding record, not a design and
not an implementation. It is cited by path from later contracts, so the filename is stable and
undated.

## Question

The re-split design binds Git execution to an open directory descriptor and passes
`--git-dir=/proc/self/fd/N` into the child, so execution follows the same inode across spawn
rather than a pathname an attacker can replace. Two governed planning rounds were BLOCKED, and
the seventh and final P0 blocker was that **this behaviour was unproven on the pinned binary**.

Every custody ticket (E1–E4) and both runners (FW, FR) inherit the assumption. This spike
measures it.

## Binary under test

| | |
| --- | --- |
| Version | `git version 2.39.5` |
| Package | `1:2.39.5-0+deb12u3` (Debian 12, amd64) |
| Path | `/usr/bin/git` |
| SHA-256 | `2540879925a6881e3877ff7e3330746ba3027b04edf16a3a12dccd1644c4f32d` |
| Kernel | Linux 6.12 (`6.12.74+deb12-amd64`), procfs mounted |

**Every result below applies only to that binary on that platform.** `tests/test_git_fd_binding_spike.py`
skips itself if the version does not match, so the results are never silently claimed for another build.

## Results

| # | Row | Result |
| --- | --- | --- |
| 1 | Provenance recorded and pinned | **PASS** |
| 2 | Listed fd inherited at the same number; others closed; stdio not aliased | **PASS** |
| 3 | procfs present; `/proc/self/fd/N` resolves to the intended directory in the child | **PASS** |
| 4 | `--git-dir=/proc/self/fd/N` binds init, blob, tree, commit, reads, object-format | **PASS** |
| 5 | Binding follows the leased inode across rename and hostile substitution | **PASS** |
| 6 | Cleanup while borrowed | **SPLIT** — fails closed for a new command; an in-flight read returns `missing` and exits 0. See below |
| 7 | User/XDG/system config suppression | **PASS**, and demonstrably necessary |
| 8 | `--no-replace-objects` in the Git-global position | **PASS**, and demonstrably necessary |
| 9 | Option terminators | **PARTIAL** — see below |

### Row 4 — every required command binds

`init --bare --object-format=sha1`, `hash-object -w --stdin`, `mktree`, `commit-tree`,
`cat-file -t/-commit/-blob`, `ls-tree -z`, and `rev-parse --show-object-format` all succeed with
`--git-dir=/proc/self/fd/N`. `mktree` accepts the fixed one-entry grammar
`100644 blob <oid>\tclaim.json` on stdin and returns a tree whose `ls-tree -z` output round-trips
byte-exactly.

### Row 5 — the property the design exists for

After the leased directory is **renamed**, the bound child still reads its own objects
(`/proc/self/fd/N` re-targets to the new path and resolution follows the inode).

After a **hostile bare repository is planted at the original pathname**, the bound child still
reads the original object and **cannot see any object the decoy contains**. Name substitution
does not redirect a bound child. This is the core custody claim, and it holds.

### Row 6 — cleanup while borrowed, and a lease does not outlive its tree

This is the finding that contradicts the design brief.

The brief assumed a borrowed duplicate descriptor keeps the repository alive until the child is
reaped. **It does not.** Git re-resolves `/proc/self/fd/N` as a *path*; once the tree is unlinked,
the magic symlink resolves to `…/claim.git (deleted)` and the entries beneath it are gone.
Measured: after the tree is unlinked, every subsequent bound command fails with
`fatal: not a git repository`. What the *already-running* child does depends on how far it had got,
which is the distinction the next paragraphs draw.

**The direction of failure is not uniform, and an earlier version of this row overstated it.** It
said the failure was always an error and that nothing silently succeeds. That holds for a command
started *after* the removal. It does not hold for a read *already in flight*.

Measured while re-contracting #122, reproduced 5/5 on the same pinned binary: if the child is first
made to answer one request -- so it is known to have opened the repository rather than presumed to --
then after the unlink its next lookup returns `<oid> missing` on **stdout** and the child **exits 0**.
It does not report the repository absent and it does not fail.

So the two cases differ:

| Case | Outcome |
| --- | --- |
| Command started after the removal | `fatal: not a git repository`, non-zero exit — fails closed |
| Read already in flight, repository already opened | `<oid> missing` on stdout, exit 0 — succeeds, reporting absence |

The original single-outcome claim came from a test that removed the tree immediately after spawning
the child, so it measured whichever of the two won a race. That race is what made the test
load-sensitive (#122), and stabilising it is what exposed the second row.

**This strengthens the cleanup requirement rather than weakening it.** The earlier reading was that a
mistimed cleanup breaks the operation loudly. The measurement says an in-flight *verification* read
can instead come back looking like a clean answer of "no such object" — which, for a protocol whose
purpose is to decide whether a claim exists, is the difference between not knowing and concluding
that nobody holds the claim. A loud break is recoverable; a confident wrong answer is not.

Consequences for the contracts:

- E4's "cleanup waits or refuses while borrowed" is **mandatory, not defensive**. Deleting during
  a borrow is not merely untidy: it breaks a new operation outright, and it makes an in-flight
  verification read return a reported absence instead of an error.
- Holding a duplicate fd is **not sufficient** to protect an in-flight operation. Whatever E2's
  borrow/refcount does, it must prevent the *unlink*, not merely keep a descriptor open.
- A refcount that permits cleanup once the last borrow is released is still correct. What is not
  correct is any claim that an open descriptor makes cleanup safe.

### Row 7 — config suppression works and is necessary

With a hostile `~/.gitconfig` reachable, `user.name` reads `HOSTILE` in the child. With
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
`HOME=/nonexistent` and `XDG_CONFIG_HOME=/nonexistent`, it is unset. The suppression set is
sufficient on this binary, and omitting it demonstrably leaks.

### Row 8 — replacement objects are a live attack, and the flag defends

With a `refs/replace/<oid>` ref present, `cat-file blob <oid>` returns the **replacement** content.
With `--no-replace-objects` in the Git-global position it returns the original. The flag is
accepted in that position by all six required commands.

Any read that verifies a claim object must carry it. Without it, an adversary with write access to
the repository can make verification read attacker-chosen bytes while the OID appears correct.

### Row 9 — terminator support is uneven, so do not design around it

Supported on the pinned binary:

- `hash-object --stdin --`
- `cat-file -t -- <oid>`
- `ls-tree -z <tree> --`
- `commit-tree <tree> -m <msg> --`

**Not supported**, and no contract may assume them:

- `git --no-replace-objects --end-of-options cat-file …` → `unknown option: --end-of-options`.
  **`--end-of-options` is not a Git-global option on 2.39.5.**
- `cat-file --end-of-options -t <oid>` → `fatal: invalid object type "-t"`.
- `rev-parse --end-of-options --show-object-format` → the query is consumed as an operand.

The PLAN_V2 wording "use the documented option terminator supported by the pinned Git command"
is therefore only partly satisfiable. **Exact typed `Sha1ObjectId` validation is the load-bearing
defence for operand injection, not the terminator.** A 40-hex lowercase operand cannot be parsed
as an option, a revision expression, or a pathspec, and that is what closes the seam.

## What this proves

For `git 2.39.5` at the digest above, on Linux 6.12 with procfs:

1. A repository can be bound through an inherited directory descriptor for the full init,
   object-write, object-read and object-format command set the claim protocol needs.
2. That binding is **not defeated by renaming or replacing the original pathname**, which is the
   security property motivating descriptor custody over a lexical path.
3. Config suppression and `--no-replace-objects` are both effective and both necessary; each
   defends against a demonstrated, not hypothetical, substitution.

## What remains unproven

Stated precisely, so no later contract over-reads this document:

- **Anything about a different Git build or platform.** No BSD, no macOS, no container without
  procfs, no other 2.39.x patch level. The test skips rather than generalising.
- **Concurrency.** Nothing here exercises two processes contending for one repository. Same-server
  contention remains #60.
- **Remote and transport behaviour.** No fetch, push, credential, ref advertisement or CAS was
  executed. Those stay under revised #50/#56/#51/#52/#58.
- **The threat model's excluded actors.** Root, `ptrace`, capability or mount-namespace authority,
  and a same-UID malicious process are all out of scope and untested. A same-UID attacker can
  close or replace the inherited descriptor, and nothing here defends that.
- **Adversary write authority over the pinned parent directory.** Row 5 tests substitution of the
  *leased child* path. It does not test an adversary who controls the parent.
- **Alternate object databases.** `objects/info/alternates` was not exercised; E3's hygiene
  preflight is still required and still unmeasured.
- **Whether the leased fd survives its own process boundary.** Every result is within one process.
  An early probe that opened the descriptor in one process and used it in another produced a
  spurious `fatal: not a git repository`; that was a broken probe, not a finding, and the final
  module runs each matrix row in a single process. Any design that hands a lease across a process
  boundary is unmeasured here.

## Decision

The spike **passes**. Repository custody by inherited descriptor is sound on the pinned binary,
so it does not need redesigning before E1–E4 and FW/FR are written.

Two corrections are mandatory in those contracts:

1. **E2/E4** must not assume a borrowed descriptor protects an in-flight operation. Cleanup has to
   prevent the unlink, and cleanup during a borrow must wait or refuse.
2. **C/D** must not rely on `--end-of-options`. Typed exact OID validation is the operand defence;
   trailing `--` is available on four commands and on none of the others.

## Reproducing

```sh
PYTHONPATH=src:. python3 -m pytest tests/test_git_fd_binding_spike.py -q
```

Ten tests, each building and removing its own bare repository under a temporary directory. No
network, no remote, no shared state, nothing outside the temporary tree.
