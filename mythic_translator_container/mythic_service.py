#!/usr/bin/env python3
import aio_pika
import sys
import json
import socket
import asyncio
import pathlib
from importlib import import_module, invalidate_caches
from functools import partial

credentials = None
connection_params = None
hostname = ""
exchange = None
debug = False


def import_all_c2_functions():
    import glob
    try:
        # Get file paths of all modules.
        modules = glob.glob("c2_functions/*.py")
        invalidate_caches()
        for x in modules:
            if not x.endswith("__init__.py") and x[-3:] == ".py":
                module = import_module("c2_functions." + pathlib.Path(x).stem, package=None)
                for el in dir(module):
                    if "__" not in el:
                        globals()[el] = getattr(module, el)
    except Exception as e:
        print("[-] import_all_c2_functions ran into an error: {}".format(str(e)))
        sys.stdout.flush()


async def rabbit_c2_rpc_callback(
    exchange: aio_pika.Exchange, message: aio_pika.IncomingMessage
):
    global debug
    with message.process():
        try:
            if debug:
                print("got message: {}".format(message.body.decode()))
                sys.stdout.flush()
            request = json.loads(message.body.decode())
            response = await globals()[request["action"]](request)
            if request["action"] != "translate_to_c2_format":
                response = json.dumps(response).encode()
            if debug:
                print("sending response: {}".format(response))
                sys.stdout.flush()
        except Exception as e:
            print("[-] Error in trying to process a message from mythic: {}".format(str(e)))
            sys.stdout.flush()
            response = b""
        try:
            if debug:
                print("sending message back to mythic")
                sys.stdout.flush()
            await exchange.publish(
                aio_pika.Message(body=response, correlation_id=message.correlation_id),
                routing_key=message.reply_to,
            )
            if debug:
                print("sent message back to mythic")
                sys.stdout.flush()
        except Exception as e:
            print(
                "[-] Exception trying to send message back to container for rpc! " + str(e)
            )
            sys.stdout.flush()


async def mythic_service(debugSetting):
    global hostname
    global exchange
    global debug
    debug = debugSetting
    connection = None
    try:
        if debug:
            print("[*] about to open rabbitmq_config.json")
            sys.stdout.flush()
        config_file = open("rabbitmq_config.json", "rb")
        main_config = json.loads(config_file.read().decode("utf-8"))
        config_file.close()
        if debug:
            print("[*] Parsed the config file")
            sys.stdout.flush()
        if main_config["name"] == "hostname":
            hostname = socket.gethostname()
        else:
            hostname = main_config["name"]
        if debug:
            print("[*] got Hostname, now to import c2 functions")
            sys.stdout.flush()
        import_all_c2_functions()
        if debug:
            print("[*] imported functions, now to start the connection loop")
            sys.stdout.flush()
        while connection is None:
            try:
                if debug:
                    print("[*] about to try to connect_robust to rabbitmq")
                    sys.stdout.flush()
                connection = await aio_pika.connect_robust(
                    host=main_config["host"],
                    login=main_config["username"],
                    password=main_config["password"],
                    virtualhost=main_config["virtual_host"],
                )
                if debug:
                    print("[*] successfully connected, now to connect to the channel")
                    sys.stdout.flush()
                channel = await connection.channel()
                # get a random queue that only the apfell server will use to listen on to catch all heartbeats
                if debug:
                    print("[*] About to declare queue")
                    sys.stdout.flush()
                queue = await channel.declare_queue("{}_rpc_queue".format(hostname), auto_delete=True)
                if debug:
                    print("[*] Declared queue")
                    sys.stdout.flush()
                await channel.set_qos(prefetch_count=50)
                try:
                    task = queue.consume(
                        partial(rabbit_c2_rpc_callback, channel.default_exchange)
                    )
                    if debug:
                        print("[*] created task to queue.consume")
                        sys.stdout.flush()
                    result = await asyncio.wait_for(task, None)
                except Exception as q:
                    if debug:
                        print("[*] Hit an exception when trying to consume the queue")
                        sys.stdout.flush()
                    print("[-] Exception in connect_and_consume .consume: {}\n trying again...".format(str(q)))
                    sys.stdout.flush()
            except (ConnectionError, ConnectionRefusedError) as c:
                if debug:
                    print("[*] hit a connection error when trying to connect to rabbitmq or the channel")
                    sys.stdout.flush()
                print("[-] Connection to rabbitmq failed, trying again...")
                sys.stdout.flush()
            except Exception as e:
                if debug:
                    print("[*] hit a generic error when trying to do initial setup and connection")
                    sys.stdout.flush()
                print("[-] Exception in connect_and_consume_rpc connect: {}\n trying again...".format(str(e)))
                # print("Exception in connect_and_consume connect: {}".format(str(e)))
                sys.stdout.flush()
            if debug:
                print("[*] Repeating outer loop while connection is None")
                sys.stdout.flush()
            await asyncio.sleep(2)
    except Exception as f:
        print("[-] Exception in main mythic_service: {}".format(str(f)))
        sys.stdout.flush()

def start_service(debug = False):
    # start our service
    if debug:
        print("[*] entered start_service")
        sys.stdout.flush()
    loop = asyncio.get_event_loop()
    if debug:
        print("[*] got event loop")
        sys.stdout.flush()
    loop.create_task(mythic_service(debug))
    if debug:
        print("[*] created task for mythic_service")
        sys.stdout.flush()
    loop.run_forever()
    if debug:
        print("[*] finished loop.run_forever()")
        sys.stdout.flush()
