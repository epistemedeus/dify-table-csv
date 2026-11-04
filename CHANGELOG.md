# Changelog

## 0.1.3

The Dify pin moves from 1.18.0 commit 50abea97062110044f15d0e03ccecadd9ccc87ca to 1.19.0 commit 8e689e868b63b6dd2b449d0eb568ca5cc7127759. DSL stays 0.7.0. The license text sha256 is unchanged. Reader core pins are unchanged: x402-url-extractor 38f8787b04bf3221ee0875ce1a6af687592c8015 and merchant commit a1dc09bb076b823fb3492ea79fa10f8602ed01a5.

Dify 1.19.0 moved service settings out of docker/.env.example into docker/envs. install/cold-install.sh now writes the 1.19.0 root env overrides and pins the image digests resolved from the official compose tags at that commit. Import, YAML export, publish, and POST /v1/workflows/run are unchanged.

proof/dify-1.19.0-run.json records the installed run, including output hashes for the public item-note sample that is not one of the older fixture URLs.
