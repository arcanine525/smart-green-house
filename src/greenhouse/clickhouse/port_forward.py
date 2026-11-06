"""Fixed-destination TCP relay for localhost access to an internal-only app.

No forward-proxy protocol, dynamic destination or application credentials.
Streamlit HTTP and WebSocket traffic share the same opaque TCP stream.
"""
import argparse
import asyncio


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError,OSError):
            pass


async def serve(host,port,listen):
    async def relay(reader,writer):
        try:
            upstream,downstream=await asyncio.wait_for(asyncio.open_connection(host,port),10)
            await asyncio.gather(pipe(reader,downstream),pipe(upstream,writer))
        except (ConnectionError,OSError,TimeoutError):
            writer.close()
    server=await asyncio.start_server(relay,'0.0.0.0',listen)
    async with server:
        await server.serve_forever()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-host',required=True)
    parser.add_argument('--target-port',type=int,required=True)
    parser.add_argument('--listen-port',type=int,default=8501)
    args=parser.parse_args()
    asyncio.run(serve(args.target_host,args.target_port,args.listen_port))
