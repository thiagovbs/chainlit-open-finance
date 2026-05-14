import asyncio
import httpx

async def descobrir_oauth_cumbuca():
    async with httpx.AsyncClient() as client:
        resposta = await client.get("https://mcp.cumbuca.com/mcp/sse")
        print(f"Status HTTP: {resposta.status_code}")
        print(f"O Segredo (WWW-Authenticate): {resposta.headers.get('www-authenticate')}")

asyncio.run(descobrir_oauth_cumbuca())