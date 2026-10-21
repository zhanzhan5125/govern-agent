# Third-party origins

Govern Agent incorporates code from [EvoAgent](https://github.com/lmnop750/EvoAgent).
The Python modules have been reorganized into `src/govern`; moving modules does
not change their origin. The imported source copy did not include a root LICENSE
file. No license grant is inferred here; verify upstream licensing before public
redistribution. Historical documentation remains in `docs/` for reference.

The TypeScript runtime uses Pi from
[earendil-works/pi](https://github.com/earendil-works/pi), pinned by
`pi-runtime/UPSTREAM.json`. Its upstream license is MIT; the vendor checkout keeps
the upstream license and is obtained by `pi-runtime/setup.mjs`. Govern's Core patch
is maintained separately in `pi-runtime/patches/govern-core.patch`.

Other dependencies retain their respective licenses, notices, and lockfile entries.
