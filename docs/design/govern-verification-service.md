# Independent local verification service

The app calls a private Unix-socket job API. Only govern-verifier has the operator-
authorized Docker socket; that socket grants broad host-equivalent container control
and remains a trusted-control-plane risk. The service has no network interfaces or
public ports and no app configuration or credentials. The main app mounts only the
communication volume read-only, not Docker.

Inputs travel as bounded JSON and a programmatically constructed tar archive. A
never-started staging container populates a fresh per-attempt Docker volume through
the Engine archive API. The workload mounts that volume read-only. There are no host
workspace bind mounts or Windows/container/daemon path translations. Each base/head
execution has its own volume. Workloads run non-root, offline, with read-only rootfs,
tmpfs work areas, no capabilities and CPU/memory/pid limits. They never receive the
Docker/service sockets, credentials, or app state.

Export profiles using PYTHONPATH=src python scripts/prepare_verifier.py. Only cached,
digest-pinned images and registered limits are allowed; the verifier never installs
dependencies or pulls images. After a profile change, export again and recreate the
service. Build/start using docker compose -f compose.govern.yaml up -d --build.

Two workers, at most four pending/running jobs, bounded payload/log storage, bounded
runtime, cancellation and unconditional cleanup protect the control plane. Startup
reaps only labelled/named verifier workloads/volumes. Unavailable service/engine never
falls back to executing repository code in the app or host.

scripts/verification/smoke_service.py runs in the app container with owned fixtures
and a disposable database. It tests real base-pass/head-fail behavior, external clock
mock, artifact persistence, read-only/non-root/credential isolation, zero tests,
missing dependencies, timeout, cancellation and cleanup. No model or product review,
feedback, policy or case-pool records are used.
