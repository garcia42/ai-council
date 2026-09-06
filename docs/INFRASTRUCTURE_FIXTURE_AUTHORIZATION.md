# Prospective fixture authorization template

Status: DRAFT_NOT_AUTHORIZED. Completing this template grants no authority by itself.
Use it before a provider or cloud fixture is provisioned; no values may be backdated.
An offline local fixture does not require cloud provisioning.

- Principal and independently retained approval reference: REQUIRED.
- Exact candidate commits, source-manifest digests and reviewed bootstrap command: REQUIRED.
- Purpose, declared proof selectors and execution UID/group transitions: REQUIRED.
- Allowed account/project, region and explicit instance/service identities: REQUIRED.
- Maximum concurrent fixtures, machine type, storage and outbound destinations: REQUIRED.
- Start time, expiry time, maximum wall duration and automatic teardown mechanism: REQUIRED.
- Dollar limit covering compute, storage, egress and conservative ambiguous exposure: REQUIRED.
- Allowed create/read/execute/delete operations, with exact resource namespace: REQUIRED.
- Prohibited operations: production stores, trading services, provider forecasting, public alert
  delivery, automatic retries of ambiguous operations and expansion outside this fixture.
- Responsible owner, stop conditions and independently observable teardown proof: REQUIRED.
- Evidence destination, retention, source bindings and missing-evidence state: REQUIRED.

Run bootstrap admission before product qualification: verify the bundle and interpreter,
inspect manifest construction order, prove parent traversal after the UID drop, preserve
Git ownership/index modes, and collect and execute one trivial test. Benign stderr is not
a failed native exit. Provisioning success is not bootstrap success; bootstrap success is
not product qualification. Refuse the expensive phase until the smoke receipt is complete.

For a timed-out create/execute/delete call, record UNKNOWN and reserve its maximum exposure.
Reconcile through a documented read-only lookup by exact resource/operation identity before
retrying. An expired deadline, missing receipt or failed teardown is a visible non-success
state. An operator cannot turn elapsed time into approval, erase an ambiguous attempt or
replay it to obtain fresher evidence. The principal's eventual approval and required council
review bind this completed envelope; neither this template nor green local tests substitute.
