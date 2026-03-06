import asyncio
import logging

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession


logging.basicConfig(level=logging.DEBUG)


async def main():
    server = StdioServerParameters(command="python3", args=[".rag/mcp_server_wrapper.py"], cwd='.')

    try:
        async with stdio_client(server) as (read, write):
            session = ClientSession(read, write)
            try:
                init = await session.initialize()
                print("INITIALIZE:", init)
            except Exception as e:
                print("initialize() failed:", repr(e))
                return

            caps = session.get_server_capabilities()
            print("SERVER CAPABILITIES:", caps)

            print("Listing resources...")
            resources = await session.list_resources()
            print("RESOURCES:", getattr(resources, 'resources', resources))
            # Skipping calling 'reindex' to avoid long-running index operations
            print("Skipping 'reindex' call (not needed for verification)")

    except Exception as e:
        print("MCP client error:", e)

if __name__ == '__main__':
    asyncio.run(main())
