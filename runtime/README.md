# Runtime setup

Use the current [README](../README.md) and
[controller usage](../docs/CONTROLLER_USAGE.md) for installation and operation.
The [dependency guide](../dependencies/README.md) describes the native build lock
and evaluator-owned `COMPRESSION_LAB_NATIVE_PREFIX`. Native libraries, Linux
sandbox support and the optional Rust toolchain remain explicit host prerequisites.

Keep the trusted evaluator, controller state and host adapter outside the agent's
write access. Model agents use the documented separate-UID public MCP boundary;
a same-UID smoke check does not establish that boundary. Prime integration and
its current verification limits are described in
[PRIME_CONTROLLER.md](../docs/PRIME_CONTROLLER.md).

The service units, AppArmor/seccomp policies, benchmark-lock rules and OCI files
in this directory are provisioning templates. Review their paths, users, runtime
versions and resource policy for the intended host before installing them.
`SETUP_0_3.md` and versioned OCI evidence describe earlier installations; they are
not current copy-and-run instructions or a claim that an image was rebuilt for
this release. Original runtime guidance and raw receipts remain in the immutable
0.5.0 archive identified by the generated `provenance/PREVIOUS_RELEASE.json` at
the application root.

Use [LOCAL_VALIDATION.md](../docs/LOCAL_VALIDATION.md) for the current tested
boundary and reproduction commands. Install a changed runtime into a new
versioned location and initialize a new workspace; preserve previous installations
and their immutable experiment evidence.
