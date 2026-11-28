# Changelog

## [0.8.1](https://github.com/yukinmax/motu-bridge/compare/v0.8.0...v0.8.1) (2026-11-28)


### Bug Fixes

* Reject integers too large for a float with 400 ([#21](https://github.com/yukinmax/motu-bridge/issues/21)) ([ce93a08](https://github.com/yukinmax/motu-bridge/commit/ce93a081ac1324f147b289927207a2e6e959e7ae))
* Reject invalid scale and choice types with 400 ([#20](https://github.com/yukinmax/motu-bridge/issues/20)) ([4e57063](https://github.com/yukinmax/motu-bridge/commit/4e5706343f56bb96a4005ee01451eb70e50bd533))
* Store written values at the motu's precision ([#18](https://github.com/yukinmax/motu-bridge/issues/18)) ([c5a72ff](https://github.com/yukinmax/motu-bridge/commit/c5a72ffcffe3c6196a38eb41023d931d6f6e6cc7))

## [0.8.0](https://github.com/yukinmax/motu-bridge/compare/0.7.0...v0.8.0) (2026-11-24)


### ⚠ BREAKING CHANGES

* Require Python 3.14 and build the Docker image on 3.14 ([#15](https://github.com/yukinmax/motu-bridge/pull/15))
