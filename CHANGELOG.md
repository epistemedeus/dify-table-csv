# Changelog

## 0.1.3

The Dify pin moves from 1.18.0 commit 50abea97062110044f15d0e03ccecadd9ccc87ca to 1.19.0 commit 8e689e868b63b6dd2b449d0eb568ca5cc7127759. DSL stays 0.7.0. The license text sha256 is unchanged. Reader core pins are unchanged: x402-url-extractor 38f8787b04bf3221ee0875ce1a6af687592c8015 and merchant commit a1dc09bb076b823fb3492ea79fa10f8602ed01a5.

Dify 1.19.0 moved service settings out of docker/.env.example into docker/envs. install/cold-install.sh now writes the 1.19.0 root env overrides and pins the image digests resolved from the official compose tags at that commit. Import, YAML export, publish, and POST /v1/workflows/run are unchanged.

proof/dify-1.19.0-run.json records the installed run, including output hashes for the public item-note sample that is not one of the older fixture URLs.

install/preflight.py lists the iptables-legacy filter FORWARD policy before an image pull. A DROP policy stops the script. The message tells the operator to set the host filter FORWARD policy to ACCEPT and re-run. The installer does not change firewall policy. When the list command is missing or not permitted, preflight prints forward_policy=unread and continues. On a host whose policy was set to DROP outside the installer, cold-install.sh exited 2 in the same second and did not pull images. The policy was still DROP after that exit. After the policy was set back to ACCEPT outside the installer, the same script exited 0 in 97 seconds with api_version 1.19.0 and installed_http passed. proof/dify-1.19.0-forward-preflight.json records both runs.

The current consumer samedaydesk at 57fa8d4b vendors a31b7954 bytes from experiments/n4-workflow-distribution-20261104/PINS.json. src/table_csv.py, workflow/reader-table-csv.dsl.yml, and LICENSE are identical to those vendored bytes. SOURCE-NOTICE.txt changed. Its a31b7954 text said dify-table-csv 0.1.0. The checker id h64314.table_csv.v1 and the provenance schema h64314.reader-csv.provenance.v1 are unchanged. The consumer needs no change unless the checker or DSL bytes changed. They did not. proof/consumer-bytes-0.1.3.json records the comparison.

The prefix-sibling preservation test plants its siblings under a temp root the test process owns. The marker bytes, mode, and refusal to delete those paths stay in the test. At a31b7954 the same test already raised PermissionError when the checkout parent was not writable.
