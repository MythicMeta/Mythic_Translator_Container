# Mythic C2 Translator Container

The `mythic_translator_container` package creates an easy way to get everything set up in a new translator container for a Mythic supported translation container. Mythic is a Command and Control (C2) framework for Red Teaming. The code is on GitHub (https://github.com/its-a-feature/Mythic) and the Mythic project's documentation is on GitBooks (https://docs.mythic-c2.net). This code will be included in the default Mythic translator containers, but is available for anybody making custom translator containers as well.

## Installation

You can install the mythic scripting interface from PyPI:

```
pip install mythic-translator-container
```

## How to use

Version 0.0.10 of the `mythic_translator_container` package supports version 2.2.2 of the Mythic project.

For the main execution of the heartbeat and service functionality, simply import and start the service:
```
from mythic_translator_container import mythic_service
mythic_service.start_service_and_heartbeat(debug=True)
```

You can also pass `debug=True` to the `start_service_and_heartbeat()` function to get detailed debugging information.

You can get the Mythic version of this package with the `get_version_info` function:
```
from mythic_translator_container.mythic_service import get_version_info
get_version_info()
```

## Where is the code?

The code for this PyPi package can be found at https://github.com/MythicMeta/Mythic_Translator_Container 

