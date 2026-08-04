# Publishing

Fafnir is published to PyPI as `fafnir-store` (`fafnir` was already taken). The import package and
the CLI are both `fafnir`.

## Release steps

1. Bump `version` in `pyproject.toml` (PyPI rejects re-uploading an existing version).
2. Build the distributions (sdist + wheel into `dist/`):

   ```sh
   uv build
   ```

3. Upload to PyPI:

   ```sh
   uv publish
   ```

`uv build` writes to `dist/`; clean it between releases (`rm -rf dist/`) so `uv publish` doesn't try
to re-upload stale builds.

Fafnir itself is a pure-Python wheel; `pyrage` ships its own abi3 wheels for macOS and Linux, so
there is nothing platform-specific to build here.

## Authentication

PyPI uses an API token (create one at <https://pypi.org/manage/account/token/>, scoped to the
`fafnir-store` project). With token auth the username is `__token__` and the token is the password,
but `uv publish` handles that for you when given a token.

`uv publish` reads the token from **`UV_PUBLISH_TOKEN`** (or the `--token` flag).

```sh
# pass it explicitly
uv publish --token "$UV_PUBLISH_TOKEN"
```
