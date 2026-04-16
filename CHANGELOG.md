# Changelog
<!-- markdownlint-disable MD013 -->
<!-- markdownlint-disable MD004 -->
<!-- markdownlint-disable MD024 -->

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

* _Nothing yet._

## [0.6.12] - 2026-04-16

### Added

* GitHub Actions workflows for test and release automation.
* Makefile `test` target to execute pytest suite in `tests/`.
* Release helper changelog updates for Keep a Changelog release sections.

## [0.6.11]

### Added

* Dynamic positioning of general parameters (for example, `-p`).

### Changed

* Improved tips for `-v` and `-p` when no discovered node is shown.

## [0.6.10]

### Added

* `discover` now prints a helpful tip if no nodes are discovered.

### Changed

* Unified internal user input prompting and callback handling using async `questionary`.

## [0.6.9]

### Fixed

* Register `set_value` no longer fails for `NaN` values (for example `float32`).
* `NaN` comparison handling now correctly treats `NaN` values during register write comparisons.

## [0.6.8]

### Added

* `cyphal update...` and `cyphal print-registry` allow selecting protocol (`fd` or `classic`) and interface.

### Fixed

* Avoided creating multiple asyncio event loops during interface selection.

## [0.6.7]

### Added

* CLI command `cyphal discover` now allows protocol (`fd` or `classic`) and interface selection.
* CLI command `cyphal discover` now supports optional configuration parameters.

## [0.6.6]

### Added

* `discover(...)` now supports `exclude_node_ids`.

## [0.6.5]

### Added

* `get_subscriber(self, port_name: str, expected_dtype: type[_T])` helper for creating a subscriber from port name and expected data type.
* `_ensure_registers(self, *names: str)` helper to ensure requested registers are present in the local registry, refreshing from device when needed.

## [0.6.4]

### Added

* Restart flow now uses uptime while waiting for node recovery.

### Fixed

* Corrected type conversion of `exclude_uid` in `discover` for `bytes` and `str` comparisons.
* `set_node_id` now awaits `uavcan.node.id` register lookup when missing from local shadow registry.

## [0.6.3]

### Fixed

* Register access after device reboot (8091935bedf024aa2e31c393ba8fee9dd4b1fed3).

## [0.6.2]

### Removed

* The `sc-packaged-dsdl` dependency introduced in [#9](https://github.com/starcopter/cyphal-device-library/pull/9).

## [0.6.1]

### Added

* Print source node ID in logged CAN diagnostic record messages.

### Changed

* Multiple minor updates for `highdra-cli` by @Finwood in [#7](https://github.com/starcopter/cyphal-device-library/pull/7).
* Refactored DSDL handling to use `sc-packaged-dsdl` by @Finwood in [#9](https://github.com/starcopter/cyphal-device-library/pull/9).

## [0.5.0]

### Added

* Device class by @Finwood in [#3](https://github.com/starcopter/cyphal-device-library/pull/3).

## [0.3.0]

### Added

* Configurable transports.

## [0.2.0]

### Added

* CI setup.
