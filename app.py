import chainlit as cl
import os
from pydantic import create_model, Field
import logging
import httpx
from contextlib import AsyncExitStack
import asyncio
import base64
import json
import re
import hashlib

# Imports oficiais e estáveis do MCP
from mcp import ClientSession
from anyio import create_memory_object_stream

# Imports do LangChain e LangGraph
from langchain_openai import ChatOpenAI
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

# =====================================================================
# CONFIGURAÇÕES (LIDAS EXCLUSIVAMENTE DO .ENV)
# =====================================================================
LLM_TOKEN_URL = os.getenv("LLM_TOKEN_URL")
LLM_CLIENT_ID = os.getenv("LLM_CLIENT_ID")
LLM_CLIENT_SECRET = os.getenv("LLM_CLIENT_SECRET")
GATEWAY_LLM_URL = os.getenv("GATEWAY_LLM_URL")
MODELO_LLM = os.getenv("MODELO_LLM", "gpt-5-nano")

MCP_TOKEN_URL = os.getenv("MCP_TOKEN_URL")
MCP_CLIENT_ID = os.getenv("MCP_CLIENT_ID")
MCP_CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET")
GATEWAY_MCP_URL = os.getenv("GATEWAY_MCP_URL")

if not all([LLM_TOKEN_URL, LLM_CLIENT_ID, LLM_CLIENT_SECRET, GATEWAY_LLM_URL,
            MCP_TOKEN_URL, MCP_CLIENT_ID, MCP_CLIENT_SECRET, GATEWAY_MCP_URL]):
    raise ValueError("❌ Erro crítico: Faltam variáveis essenciais no arquivo .env para a Sensedia!")

class IgnorarKeepaliveMCP(logging.Filter):
    def filter(self, record):
        mensagem = record.getMessage()
        if "Failed to validate notification" in mensagem and "$/keepalive" in mensagem:
            return False
        return True

logging.getLogger().addFilter(IgnorarKeepaliveMCP())

# =====================================================================
# FUNÇÃO DE AUTENTICAÇÃO PADRÃO KEYCLOAK
# =====================================================================
async def obter_token_keycloak(token_url: str, client_id: str, client_secret: str) -> str:
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    async with httpx.AsyncClient() as client:
        resposta = await client.post(token_url, data=payload, headers=headers)
        if resposta.status_code != 200:
            print(f"[ERRO KEYCLOAK] URL: {token_url} | Código: {resposta.status_code}")
            resposta.raise_for_status()
        return resposta.json()["access_token"]

# =====================================================================
# COMPRESSOR E TRATAMENTO DE PAYLOAD (ANTIALUCINAÇÃO)
# =====================================================================
def gerar_nome_seguro_llm(nome_original: str) -> str:
    nome_limpo = re.sub(r'[^a-zA-Z0-9_-]', '_', nome_original)
    if len(nome_limpo) <= 64:
        return nome_limpo
    hash_curto = hashlib.md5(nome_original.encode()).hexdigest()[:8]
    return f"{nome_limpo[:54]}_{hash_curto}"

def truncar_descricao(texto: str, limite: int = 100) -> str:
    if not texto:
        return "Sem descrição"
    texto_limpo = str(texto).replace('\n', ' ').strip()
    if len(texto_limpo) > limite:
        return texto_limpo[:limite] + "..."
    return texto_limpo

# =====================================================================
# MOTOR DE INICIALIZAÇÃO
# =====================================================================
async def inicializar_agente_sistema():
    exit_stack = AsyncExitStack()
    
    print("\n🚀 [V3] NOVO CÓDIGO COM FILTRO ANTIALUCINAÇÃO RODANDO! 🚀\n")
    print("[MCP] Solicitando token de acesso ao Keycloak...")
    token_mcp = await obter_token_keycloak(MCP_TOKEN_URL, MCP_CLIENT_ID, MCP_CLIENT_SECRET)

    headers_mcp = {
        "Authorization": f"Bearer {token_mcp.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0"
    }
    
    meu_cliente_http = httpx.AsyncClient(headers=headers_mcp, timeout=30.0)
    await exit_stack.enter_async_context(meu_cliente_http)

    print("[MCP] Executando Handshake inicial...")
    payload_handshake = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 0,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Chainlit Bot", "version": "1.0.0"}
        }
    }

    resposta_init = await meu_cliente_http.post(GATEWAY_MCP_URL, json=payload_handshake)
    if resposta_init.status_code != 200:
        raise RuntimeError(f"Falha no handshake inicial com a Sensedia: {resposta_init.text}")

    id_sessao_detectado = "0"
    for chave, valor in resposta_init.headers.items():
        if "session" in chave.lower() or "id" in chave.lower():
            id_sessao_detectado = valor

    headers_mcp["X-MCP-Session-ID"] = id_sessao_detectado
    headers_mcp["X-Session-ID"] = id_sessao_detectado
    headers_mcp["Mcp-Session-Id"] = id_sessao_detectado
    headers_mcp["Session-Id"] = id_sessao_detectado

    payload_tools = {"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}}
    resposta_tools = await meu_cliente_http.post(GATEWAY_MCP_URL, json=payload_tools, headers=headers_mcp)
    
    if resposta_tools.status_code == 404:
        resposta_tools = await meu_cliente_http.post(f"{GATEWAY_MCP_URL.rstrip('/')}/messages", json=payload_tools, headers=headers_mcp)

    if resposta_tools.status_code != 200:
        raise RuntimeError(f"Falha ao listar ferramentas no Gateway: {resposta_tools.text}")

    texto_tools = resposta_tools.text.strip().replace("data:", "").strip()
    dados_tools = json.loads(texto_tools)
    resultado_mcp = dados_tools.get("result", {})
    
    lista_ferramentas_puras = resultado_mcp[0].get("tools", []) if isinstance(resultado_mcp, list) else resultado_mcp.get("tools", [])

    # LIMITADOR EXTREMO
    LIMITE_EXTREMO = 3
    ferramentas_selecionadas = lista_ferramentas_puras[:LIMITE_EXTREMO]
    print(f"[SISTEMA] Carregando APENAS {len(ferramentas_selecionadas)} ferramentas blindadas na LLM.\n")

    # 💡 O PULO DO GATO 2: Recebemos o nome_seguro para travar a biblioteca do LangChain
    def criar_executor_ferramenta(nome_original, nome_seguro):
        async def executar_ferramenta_sensedia(**kwargs):
            payload_call = {"jsonrpc": "2.0", "method": "tools/call", "id": 99, "params": {"name": nome_original, "arguments": kwargs}}
            try:
                res = await meu_cliente_http.post(GATEWAY_MCP_URL, json=payload_call, headers=headers_mcp)
                if res.status_code == 404:
                    res = await meu_cliente_http.post(f"{GATEWAY_MCP_URL.rstrip('/')}/messages", json=payload_call, headers=headers_mcp)
                
                if res.status_code == 200:
                    txt = res.text.strip().replace("data:", "").strip()
                    obj = json.loads(txt)
                    content_list = obj.get("result", {}).get("content", [])
                    return "\n".join([c.get("text", "") for c in content_list if "text" in c])
                return f"Erro: {res.status_code}"
            except Exception as e:
                return f"Falha ao executar ferramenta: {str(e)}"
        
        # Injeção de segurança: Sobrescrevemos o nome interno da função para a LLM nunca conseguir puxá-lo!
        executar_ferramenta_sensedia.__name__ = nome_seguro
        return executar_ferramenta_sensedia

    ferramentas_langchain = []
    for t in ferramentas_selecionadas:
        nome_original = t.get("name")
        nome_seguro = gerar_nome_seguro_llm(nome_original)
        
        # 💡 O PULO DO GATO 3: Varremos a descrição e apagamos qualquer menção ao nome gigante
        descricao_bruta = str(t.get("description", "Sem descrição"))
        descricao_limpa = descricao_bruta.replace(nome_original, nome_seguro)
        descricao_comprimida = truncar_descricao(descricao_limpa, 80)
        
        schema_input = t.get("inputSchema", {})
        campos_pydantic = {}
        
        for nome_param, detalhes in schema_input.get("properties", {}).items():
            tipo_json = detalhes.get("type", "string")
            tipo_py = str if tipo_json == "string" else (int if tipo_json == "integer" else (float if tipo_json == "number" else bool))
            
            # Limpa o nome gigante de dentro da descrição dos parâmetros também
            desc_param_bruta = str(detalhes.get("description", ""))
            desc_param_limpa = desc_param_bruta.replace(nome_original, nome_seguro)
            desc_param_final = truncar_descricao(desc_param_limpa, 50)
            
            if nome_param in schema_input.get("required", []):
                campos_pydantic[nome_param] = (tipo_py, Field(..., description=desc_param_final))
            else:
                campos_pydantic[nome_param] = (tipo_py, Field(default=None, description=desc_param_final))

        EsquemaDinamico = create_model(f"{nome_seguro}Schema", **campos_pydantic)
        ferramentas_langchain.append(
            StructuredTool.from_function(
                name=nome_seguro,
                description=descricao_comprimida,
                coroutine=criar_executor_ferramenta(nome_original, nome_seguro), # Passamos o seguro
                args_schema=EsquemaDinamico
            )
        )

    print("[LLM] Solicitando token de acesso ao Keycloak...")
    token_llm = await obter_token_keycloak(LLM_TOKEN_URL, LLM_CLIENT_ID, LLM_CLIENT_SECRET)
    
    headers_llm_sensedia = {
        "Authorization": f"Bearer {token_llm.strip()}",
        "Content-Type": "application/json"
    }

    llm = ChatOpenAI(
        model=MODELO_LLM, 
        base_url=GATEWAY_LLM_URL.strip().rstrip('/'),
        api_key=token_llm.strip(), 
        default_headers=headers_llm_sensedia
    )
    
    agente = create_react_agent(llm, tools=ferramentas_langchain)
    
    cl.user_session.set("exit_stack", exit_stack)
    cl.user_session.set("agente_langgraph", agente)
    print("[SISTEMA] Inicialização concluída com sucesso!")

# =====================================================================
# EVENTOS DO CHAINLIT
# =====================================================================
@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("historico_mensagens", [])
    msg_status = cl.Message(content="⚙️ *Estabelecendo conexão segura com os barramentos Sensedia...*")
    await msg_status.send()

    try:
        await inicializar_agente_sistema()
        msg_status.content = "🏦 **Assistente Open Finance Corporativo Conectado!**\n\nEstou pronto para ajudar. Como você quer começar?"
        await msg_status.update()
    except Exception as e:
        msg_status.content = f"❌ **Falha na inicialização do sistema:** {str(e)}"
        await msg_status.update()

@cl.on_message
async def main(message: cl.Message):
    agente = cl.user_session.get("agente_langgraph")
    if not agente:
        await cl.Message(content="❌ Sistema não conectado.").send()
        return

    msg_chainlit = cl.Message(content="")
    await msg_chainlit.send()

    historico = cl.user_session.get("historico_mensagens", [])
    historico.append(HumanMessage(content=message.content))
    config = {"configurable": {"thread_id": cl.user_session.get("id")}}

    try:
        print("[DEBUG] Processando o fluxo com a LLM...")
        async for event in agente.astream_events({"messages": historico}, config, version="v2"):
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"].content
                if chunk:
                    await msg_chainlit.stream_token(chunk)
        
        historico.append(msg_chainlit.content)
        cl.user_session.set("historico_mensagens", historico)
        await msg_chainlit.update()

    except Exception as e:
        await cl.Message(content=f"❌ **Erro inesperado durante o processamento:** {str(e)}").send()

@cl.on_chat_end
async def on_chat_end():
    exit_stack = cl.user_session.get("exit_stack")
    if exit_stack:
        try:
            await exit_stack.aclose()
        except:
            pass