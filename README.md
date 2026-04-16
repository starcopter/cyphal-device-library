# Cyphal Device Library

This library contains a set of tools to simplify interaction with Cyphal devices.

## How to contribute

This project follows the [GitFlow][gitflow] branching model. In short, this means:

- Regular development happens on the `develop` branch.
- The `main` branch is the latest stable release; release tags are only created on this branch.
- Feature, bugfix and release branches are created from `develop`, hotfix branches are created from `main`.
- Release and hotfix branches merge back into `main` (PR only), where the merge commit shall create a release.
  _After_ the merge, the tagged release (not the release/hotfix branch) should be merged back into `develop`[^1].

[gitflow]: https://nvie.com/posts/a-successful-git-branching-model/
[^1]: This is a slight deviation from the original GitFlow model as [described by Vincent Driessen][gitflow].

### Create a new Release (from develop branch)

1. Open the GitHub Action page
2. Select the `Release CI` Workflow
3. Select branch: __develop__
4. Enter release version: e.g. __0.1.1__
5. Start workflow and wait for finish of test, changelog updates
6. A PR is created -> Review this and merge it to main
7. The merge triggers the second part of the CI -> upload of release artifacts and tag creation

### Hotfixing

1. Open the GitHub Action page
2. Select the `Hotfix CI` Workflow, run on _main_ branch
3. Edit the new hotfix branch and edit the two PR

## How to use

Create a new python project (e.g. using `uv`):
`uv init`

Add this as dependency:
`uv add "cyphal-device-library>=0.6.12" git+https://github.com/starcopter/cyphal-device-library`

Update the pycyphal message DSDLs:
`uv run cyphal install`

Use the uv .venv:
`source .venv/bin/activate`

### Example pyproject.toml

```python
[project]
name = "repos"
version = "0.1.0"
description = "Add your description here"
readme = "README.md"
requires-python = ">=3.14"
dependencies = [
    "cyphal-device-library>=0.6.12",
]

[tool.uv.sources]
cyphal-device-library = { git = "https://github.com/starcopter/cyphal-device-library" }

```
