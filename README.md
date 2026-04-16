# Cyphal Device Library

This library contains a set of tools to simplify interaction with Cyphal devices.


## How to use

Create a new python project (e.g. using `uv`):  
`uv init`

Add this as dependecy:  
`uv add "cyphal-device-library>=0.6.12" git+https://github.com/starcopter/cyphal-device-library`

### Example pyproject.toml

```
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
