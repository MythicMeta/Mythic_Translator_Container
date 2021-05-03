#!/usr/bin/env python3
import aio_pika
import sys
import json
import socket
import asyncio
import pathlib
from importlib import import_module, invalidate_caches
from functools import partial
from .config import settings

credentials = None
connection_params = None
hostname = ""
exchange = None
debug = False

container_version = "3"

def print_flush(msg):
    print(msg)
    sys.stdout.flush()

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
        print_flush("[-] import_all_c2_functions ran into an error: {}".format(str(e)))
        sys.exit(1)


async def rabbit_c2_rpc_callback(
    exchange: aio_pika.Exchange, message: aio_pika.IncomingMessage
):
    global debug
    with message.process():
        try:
            if debug:
                print_flush("got message: {}".format(message.body.decode()))
            request = json.loads(message.body.decode())
            if request["action"] == "exit_container":
                print_flush("[*] Got exit container command, exiting!")
                sys.exit(1)
            response = await globals()[request["action"]](request)
            if request["action"] != "translate_to_c2_format":
                response = json.dumps(response).encode()
            if debug:
                print_flush("sending response: {}".format(response))
        except Exception as e:
            print_flush("[-] Error in trying to process a message from mythic: {}".format(str(e)))
            response = b""
        try:
            if debug:
                print_flush("sending message back to mythic")
            await exchange.publish(
                aio_pika.Message(body=response, correlation_id=message.correlation_id),
                routing_key=message.reply_to,
            )
            if debug:
                print_flush("sent message back to mythic")
        except Exception as e:
            print_flush(
                "[-] Exception trying to send message back to container for rpc! " + str(e)
            )


async def mythic_service(debugSetting):
    global hostname
    global exchange
    global debug
    debug = debugSetting
    connection = None
    try:
        hostname = settings.get("name", "hostname")
        if hostname == "hostname":
            hostname = socket.gethostname()
        if debug:
            print_flush("[*] got Hostname, now to import c2 functions")
        import_all_c2_functions()
        if debug:
            print_flush("[*] imported functions, now to start the connection loop")
        while connection is None:
            try:
                if debug:
                    print_flush("[*] about to try to connect_robust to rabbitmq")
                connection = await aio_pika.connect_robust(
                    host=settings.get("host", "127.0.0.1"),
                    login=settings.get("username", "mythic_user"),
                    password=settings.get("password", "mythic_password"),
                    virtualhost=settings.get("virtual_host", "mythic_vhost"),
                )
                if debug:
                    print_flush("[*] successfully connected, now to connect to the channel")
                channel = await connection.channel()
                # get a random queue that only the apfell server will use to listen on to catch all heartbeats
                if debug:
                    print_flush("[*] About to declare queue")
                queue = await channel.declare_queue("{}_rpc_queue".format(hostname), auto_delete=True)
                if debug:
                    print_flush("[*] Declared queue")
                await channel.set_qos(prefetch_count=50)
                try:
                    task = queue.consume(
                        partial(rabbit_c2_rpc_callback, channel.default_exchange)
                    )
                    if debug:
                        print_flush("[*] created task to queue.consume")
                    print_flush("[+] Successfully connected to rabbitmq queue for RPC with container version " + container_version)
                except Exception as q:
                    if debug:
                        print_flush("[*] Hit an exception when trying to consume the queue")
                    print_flush("[-] Exception in connect_and_consume .consume: {}\n trying again...".format(str(q)))
            except (ConnectionError, ConnectionRefusedError) as c:
                if debug:
                    print_flush("[*] hit a connection error when trying to connect to rabbitmq or the channel")
                print_flush("[-] Connection to rabbitmq failed, trying again...")
            except Exception as e:
                if debug:
                    print_flush("[*] hit a generic error when trying to do initial setup and connection")
                print_flush("[-] Exception in connect_and_consume_rpc connect: {}\n trying again...".format(str(e)))
            if debug:
                print_flush("[*] Repeating outer loop while connection is None")
            await asyncio.sleep(2)
    except Exception as f:
        print_flush("[-] Exception in main mythic_service, exiting: {}".format(str(f)))
        sys.exit(1)

async def heartbeat_loop(debug: bool):
    connection = None
    hostname = settings.get("name", "hostname")
    if hostname == "hostname":
        hostname = socket.gethostname()
    while connection is None:
        try:
            if debug:
                print_flush("Connecting to rabbitmq from heartbeat_loop")
            connection = await aio_pika.connect_robust(
                host=settings.get("host", "127.0.0.1"),
                login=settings.get("username", "mythic_user"),
                password=settings.get("password", "mythic_password"),
                virtualhost=settings.get("virtual_host", "mythic_vhost"),
            )
            if debug:
                print_flush("Successfully connected to rabbitmq from heartbeat_loop")
            channel = await connection.channel()
            if debug:
                print_flush("Declaring exchange in heartbeat_loop for channel")
            exchange = await channel.declare_exchange(
                "mythic_traffic", aio_pika.ExchangeType.TOPIC
            )
        except (ConnectionError, ConnectionRefusedError) as c:
            print_flush("Connection to rabbitmq failed, trying again...")
            await asyncio.sleep(3)
            continue
        except Exception as e:
            print_flush("Exception in heartbeat_loop: " + str(e))
            await asyncio.sleep(3)
            continue
        print_flush("[+] Successfully connected to rabbitmq for sending heartbeats")
        while True:
            try:
                # routing key is ignored for fanout, it'll go to anybody that's listening, which will only be the server
                if debug:
                    print_flush("Sending heartbeat in heartbeat_loop")
                await exchange.publish(
                    aio_pika.Message("heartbeat".encode()),
                    routing_key="tr.heartbeat.{}.{}".format(hostname, container_version),
                )
                await asyncio.sleep(10)
            except Exception as e:
                print_flush("Exception in heartbeat_loop trying to send heartbeat: " + str(e))
                # if we get an exception here, break out to the bigger loop and try to connect again
                break

def start_service_and_heartbeat(debug = False):
    # start our service
    if debug:
        print_flush("[*] entered start_service")
    loop = asyncio.get_event_loop()
    if debug:
        print_flush("[*] got event loop")
    if debug:
        print_flush("[*] created task for mythic_service and heartbeat")
    asyncio.gather(mythic_service(debug), heartbeat_loop(debug))  
    loop.run_forever()
    if debug:
        print_flush("[*] finished loop.run_forever()")

def get_version_info():
    print_flush("[*] Mythic Translator Version: " + container_version)
