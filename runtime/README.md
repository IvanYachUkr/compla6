# Runtime setup

Use the [installation guide](../docs/INSTALL.md) and
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

For the optional OCI build, prepare the untracked wheel inputs on Linux x86-64
with CPython 3.12, then run the build script from the repository root:

```sh
python3.12 -m pip wheel --no-deps --wheel-dir dist .
python3.12 -m pip download --only-binary=:all: --require-hashes \
  -r requirements-mcp.lock --dest wheelhouse/mcp-linux-x86_64-cp312
sh runtime/build-oci.sh
```

These templates pin their own Ubuntu/toolchain inputs. Building an image does
not validate a deployment's user separation, resource limits, or native codec
prefix. Provision and verify those prerequisites on the intended host.
