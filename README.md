# Mythic C2 Translator Container

The `mythic_translator_container` package creates an easy way to get everything set up in a new C2 translator container for a Mythic supported C2 Profile. Mythic is a Command and Control (C2) framework for Red Teaming. The code is on GitHub (https://github.com/its-a-feature/Mythic) and the Mythic project's documentation is on GitBooks (https://docs.mythic-c2.net). This code will be included in the default Mythic C2 translator containers, but is available for anybody making custom translator containers as well.

## Installation

You can install the mythic scripting interface from PyPI:

```
pip install mythic-translator-container
```

## How to use

Version 0.0.1 of the `mythic_translator_container` package supports version 2.2.* of the Mythic project.

For the main execution of the heartbeat and service functionality, simply import and start the service:
```
from mythic_translator_container import mythic_service
mythic_service.start_service()
```

For a C2 Profile's code to leverage the C2ProfileBase or RPC functionality:
```
from mythic_translator_container import C2ProfileBase
from mythic_translator_container import MythicCallbackRPC
```
