#!/usr/bin/env python3

import sys
import argparse
import asyncio

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.lexers import PygmentsLexer

from .api import ResponseType, rpc, klipper_call, send_gcode, \
    receive_task, shared
from .lexer import style, KlipperLexer
from .output import print_output, render_output


async def run_repl(sock_write, session, prompt):
    completion = WordCompleter(shared.macro_list, ignore_case=True)
    lexer = PygmentsLexer(KlipperLexer)

    with patch_stdout():
        while True:
            try:
                line = await session.prompt_async(prompt, completer=completion, lexer=lexer)
            except KeyboardInterrupt:
                raise EOFError()
            await send_gcode(sock_write, line)

async def run(args):
    first_connect = True
    first_reconnect = True
    loop = asyncio.get_event_loop()

    interactive = len(args.gcode) == 0 or args.interactive
    if interactive:
        stdin_read = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin_read),
                                    sys.stdin)
        session = PromptSession(complete_style=CompleteStyle.READLINE_LIKE, style=style)

    while True:
        try:
            sock_read, sock_write = await asyncio.open_unix_connection(args.socket)
        except (ConnectionError, FileNotFoundError) as e:
            if first_connect:
                print_output(f'## Failed to connect: {e}')
                return 1
            if first_reconnect:
                print_output('## Waiting for Klipper...')
                first_reconnect = False
            await asyncio.sleep(0.25)
            continue

        rpcs = [
            rpc('info', id=ResponseType.Info, params={
                'client_info': { 'version': 'v1' }
            }),
            rpc('gcode/subscribe_output', key=ResponseType.Gcode)
        ]
        if interactive:
            rpcs.append(rpc('gcode/help', id=ResponseType.Macros))
            handle_gcode = render_output
        else:
            async def handle_gcode(res):
                await render_output(res)
                raise asyncio.CancelledError()

        connect_event = asyncio.Event()
        macro_event = asyncio.Event()
        recv_task = loop.create_task(receive_task(sock_read,
                                                  connect_event,
                                                  macro_event,
                                                  handle_gcode))
        await klipper_call(sock_write, rpcs)
        await connect_event.wait()

        if len(args.gcode) > 0:
            await send_gcode(sock_write, ' '.join(args.gcode))
        if not interactive:
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
            finally:
                sock_write.close()
                await sock_write.wait_closed()
            return 0

        first_reconnect = True
        hostname = shared.connection_info.get('hostname')
        if first_connect:
            print_output(f'## Connected to Klipper at {hostname}:{args.socket}\n   ^C or ^D to quit; type M112 for emergency stop')
            first_connect = False
        else:
            print_output('## Reconnected to Klipper')
        await macro_event.wait()

        prompt_task = loop.create_task(run_repl(sock_write, session, f'{hostname}:{args.socket}* '))
        def disconnect_handler(recv_task):
            prompt_task.cancel()
            if not recv_task.cancelled():
                e = recv_task.exception()
                if e is not None:
                    print_output(f'## Disconnected: {e}')
        recv_task.add_done_callback(disconnect_handler)

        try:
            await prompt_task
        except EOFError:
            recv_task.cancel()
            return 0
        except asyncio.CancelledError as e:
            # Canceled from disconnect_handler
            continue
        finally:
            sock_write.close()
            await sock_write.wait_closed()


def main():
    parser = argparse.ArgumentParser(description='A Klipper g-code command line.')
    parser.add_argument('-i', '--interactive', action='store_true',
                        help='Force interactive session if gcode is specified')
    parser.add_argument('socket', help='The Klipper API socket to connect to')
    parser.add_argument('gcode', nargs='*', help='G-code to send to Klipper')

    if len(sys.argv) < 2:
        parser.print_usage()
        sys.exit(2)
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()
