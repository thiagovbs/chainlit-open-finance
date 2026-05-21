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
import time

# Imports do LangChain e LangGraph
from langchain_openai import ChatOpenAI
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, SystemMessage

# =====================================================================
# CONFIGURAÇÕES
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

class IgnorarKeepaliveMCP(logging.Filter):
    def filter(self, record): return False if "$/keepalive" in record.getMessage() else True
logging.getLogger().addFilter(IgnorarKeepaliveMCP())

async def obter_token_keycloak(token_url: str, client_id: str, client_secret: str) -> str:
    payload = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as client:
        res = await client.post(token_url, data=payload, headers=headers)
        res.raise_for_status()
        return res.json()["access_token"]

def gerar_nome_seguro_llm(nome_original: str) -> str:
    nome_limpo = re.sub(r'[^a-zA-Z0-9_-]', '_', nome_original)
    if len(nome_limpo) <= 64: return nome_limpo
    return f"{nome_limpo[:54]}_{hashlib.md5(nome_original.encode()).hexdigest()[:8]}"

def truncar_descricao(texto: str, limite: int = 150) -> str:
    if not texto: return "Sem descrição"
    texto_limpo = str(texto).replace('\n', ' ').strip()
    return texto_limpo[:limite] + "..." if len(texto_limpo) > limite else texto_limpo

# =====================================================================
# MOTOR DE TOKEN REFRESH (PROATIVO)
# =====================================================================
async def verificar_renovacao_tokens():
    ultimo_refresh = cl.user_session.get("token_timestamp", 0)
    agora = time.time()
    
    if agora - ultimo_refresh > 240:
        print("\n[SECURITY] ⏳ Token próximo de expirar. Executando Refresh Automático...")
        
        token_mcp = await obter_token_keycloak(MCP_TOKEN_URL, MCP_CLIENT_ID, MCP_CLIENT_SECRET)
        token_llm = await obter_token_keycloak(LLM_TOKEN_URL, LLM_CLIENT_ID, LLM_CLIENT_SECRET)
        
        headers_mcp = cl.user_session.get("headers_mcp")
        if headers_mcp:
            headers_mcp["Authorization"] = f"Bearer {token_mcp.strip()}"
        
        nova_llm = ChatOpenAI(
            model=MODELO_LLM, 
            base_url=GATEWAY_LLM_URL.strip().rstrip('/'),
            api_key=token_llm.strip(), 
            default_headers={"Authorization": f"Bearer {token_llm.strip()}", "Content-Type": "application/json"}
        )
        
        cl.user_session.set("llm_base", nova_llm)
        cl.user_session.set("token_timestamp", agora)
        print("[SECURITY] ✅ Tokens renovados com sucesso!\n")

# =====================================================================
# INICIALIZAÇÃO (CATALOGAÇÃO)
# =====================================================================
async def inicializar_sistema():
    exit_stack = AsyncExitStack()
    print("\n🚀 [V8] SMART ROUTING + AUTO TOKEN REFRESH + PAYLOAD DEBUG ATIVADOS! 🚀\n")
    
    token_mcp = await obter_token_keycloak(MCP_TOKEN_URL, MCP_CLIENT_ID, MCP_CLIENT_SECRET)
    headers_mcp = {
        "Authorization": f"Bearer {token_mcp.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    
    cliente_http = httpx.AsyncClient(headers=headers_mcp, timeout=30.0)
    await exit_stack.enter_async_context(cliente_http)

    payload_init = {"jsonrpc": "2.0", "method": "initialize", "id": 0, "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "Bot", "version": "1"}}}
    res_init = await cliente_http.post(GATEWAY_MCP_URL, json=payload_init)
    
    id_sessao = next((v for k, v in res_init.headers.items() if "session" in k.lower() or "id" in k.lower()), "0")
    for header in ["X-MCP-Session-ID", "X-Session-ID", "Mcp-Session-Id", "Session-Id"]:
        headers_mcp[header] = id_sessao

    payload_tools = {"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}}
    res_tools = await cliente_http.post(GATEWAY_MCP_URL, json=payload_tools, headers=headers_mcp)
    if res_tools.status_code == 404:
        res_tools = await cliente_http.post(f"{GATEWAY_MCP_URL.rstrip('/')}/messages", json=payload_tools, headers=headers_mcp)

    txt = res_tools.text.strip().replace("data:", "").strip()
    resultado = json.loads(txt).get("result", {})
    catalogo_bruto = resultado[0].get("tools", []) if isinstance(resultado, list) else resultado.get("tools", [])

    catalogo_texto_para_llm = ""
    for t in catalogo_bruto:
        nome_seguro = gerar_nome_seguro_llm(t.get("name"))
        desc = truncar_descricao(t.get("description"), 100)
        catalogo_texto_para_llm += f"- {nome_seguro}: {desc}\n"

    print(f"[SISTEMA] Catálogo indexado com {len(catalogo_bruto)} ferramentas prontas para roteamento.")

    token_llm = await obter_token_keycloak(LLM_TOKEN_URL, LLM_CLIENT_ID, LLM_CLIENT_SECRET)
    llm = ChatOpenAI(
        model=MODELO_LLM, 
        base_url=GATEWAY_LLM_URL.strip().rstrip('/'),
        api_key=token_llm.strip(), 
        default_headers={"Authorization": f"Bearer {token_llm.strip()}", "Content-Type": "application/json"}
    )
    
    cl.user_session.set("exit_stack", exit_stack)
    cl.user_session.set("cliente_http", cliente_http)
    cl.user_session.set("headers_mcp", headers_mcp)
    cl.user_session.set("llm_base", llm)
    cl.user_session.set("catalogo_bruto", catalogo_bruto)
    cl.user_session.set("catalogo_texto", catalogo_texto_para_llm)
    cl.user_session.set("token_timestamp", time.time())

# =====================================================================
# EVENTOS DO CHAINLIT E MOTOR DE ROTEAMENTO DINÂMICO
# =====================================================================
@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("historico_mensagens", [])
    msg = cl.Message(content="⚙️ *Indexando ferramentas Open Finance e conectando...*")
    await msg.send()
    try:
        await inicializar_sistema()
        msg.content = "🏦 **Assistente Open Finance Conectado via Smart Router!**\n\nComo posso ajudar?"
        await msg.update()
    except Exception as e:
        msg.content = f"❌ Erro: {str(e)}"
        await msg.update()

@cl.on_message
async def main(message: cl.Message):
    await verificar_renovacao_tokens()
    
    llm = cl.user_session.get("llm_base")
    catalogo_bruto = cl.user_session.get("catalogo_bruto")
    catalogo_texto = cl.user_session.get("catalogo_texto")
    cliente_http = cl.user_session.get("cliente_http")
    headers_mcp = cl.user_session.get("headers_mcp")
    
    if not llm:
        await cl.Message(content="❌ Sistema não conectado.").send()
        return

    msg_chainlit = cl.Message(content="🤔 *Analisando qual ferramenta utilizar...*")
    await msg_chainlit.send()

    # -----------------------------------------------------------------
    # PASSO 1: O ROTEADOR 
    # -----------------------------------------------------------------
    prompt_roteador = f"""Você é um roteador de APIs. Baseado na mensagem do usuário, decida quais ferramentas são estritamente necessárias para responder.
Responda APENAS com os nomes das ferramentas exatos separados por vírgula. Se nenhuma ferramenta for necessária, responda NENHUMA.

Catálogo de ferramentas disponíveis:
{catalogo_texto}

Mensagem do usuário: {message.content}"""

    resposta_roteador = await llm.ainvoke([HumanMessage(content=prompt_roteador)])
    nomes_escolhidos_texto = resposta_roteador.content.strip()
    
    print(f"[ROUTER] A LLM solicitou as seguintes ferramentas: {nomes_escolhidos_texto}")

    # -----------------------------------------------------------------
    # PASSO 2: MONTANDO APENAS AS FERRAMENTAS SOLICITADAS
    # -----------------------------------------------------------------
    nomes_escolhidos = [n.strip() for n in nomes_escolhidos_texto.split(',')]
    ferramentas_langchain = []
    ferramentas_dump = [] # <-- Armazena o schema das ferramentas escolhidas para debug

    def criar_executor(nome_original, nome_seguro):
        async def executar(**kwargs):
            payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 99, "params": {"name": nome_original, "arguments": kwargs}}
            try:
                # Dispara a requisição real para o Gateway do Open Finance
                res = await cliente_http.post(GATEWAY_MCP_URL, json=payload, headers=headers_mcp)
                if res.status_code == 404: 
                    res = await cliente_http.post(f"{GATEWAY_MCP_URL.rstrip('/')}/messages", json=payload, headers=headers_mcp)
                
                if res.status_code == 200:
                    obj = json.loads(res.text.strip().replace("data:", "").strip())
                    texto_completo = "\n".join([c.get("text", "") for c in obj.get("result", {}).get("content", []) if "text" in c])
                    
                    # 💡 O PULO DO GATO 5: O Cortador de Respostas Gigantes!
                    # Evita que a resposta monstruosa do banco quebre a próxima ida para a LLM
                    LIMITE_RESPOSTA = 6000 # Caracteres seguros para o Gateway da Sensedia
                    if len(texto_completo) > LIMITE_RESPOSTA:
                        print(f"\n[AVISO] Resposta da ferramenta '{nome_seguro}' era muito grande ({len(texto_completo)} chars). Truncando para {LIMITE_RESPOSTA}...\n")
                        return texto_completo[:LIMITE_RESPOSTA] + "\n\n...[AVISO DO SISTEMA: Resposta da API truncada por limite de tamanho. Use os dados acima para guiar o usuário.]"
                    
                    return texto_completo
                return f"Erro: {res.status_code}"
            except Exception as e: 
                return f"Erro: {str(e)}"
                
        executar.__name__ = nome_seguro
        return executar

    for t in catalogo_bruto:
        nome_original = t.get("name")
        nome_seguro = gerar_nome_seguro_llm(nome_original)
        
        if nome_seguro in nomes_escolhidos:
            desc = truncar_descricao(t.get("description", "").replace(nome_original, nome_seguro), 100)
            schema_input = t.get("inputSchema", {})
            campos = {}
            for param, det in schema_input.get("properties", {}).items():
                tipo_py = str if det.get("type", "string") == "string" else int
                desc_p = truncar_descricao(det.get("description", "").replace(nome_original, nome_seguro), 60)
                if param in schema_input.get("required", []):
                    campos[param] = (tipo_py, Field(..., description=desc_p))
                else:
                    campos[param] = (tipo_py, Field(default=None, description=desc_p))
            
            esquema = create_model(f"{nome_seguro}Schema", **campos)
            ferramenta_estruturada = StructuredTool.from_function(name=nome_seguro, description=desc, coroutine=criar_executor(nome_original, nome_seguro), args_schema=esquema)
            
            ferramentas_langchain.append(ferramenta_estruturada)
            ferramentas_dump.append({
                "name": ferramenta_estruturada.name,
                "description": ferramenta_estruturada.description,
                "parameters": ferramenta_estruturada.args_schema.schema() if ferramenta_estruturada.args_schema else {}
            })

    # -----------------------------------------------------------------
    # PASSO 3: O EXECUTOR (COM INTERCEPTADOR DE ERROS)
    # -----------------------------------------------------------------
    historico = cl.user_session.get("historico_mensagens", [])
    historico.append(HumanMessage(content=message.content))
    config = {"configurable": {"thread_id": cl.user_session.get("id")}}

    agente = create_react_agent(llm, tools=ferramentas_langchain if ferramentas_langchain else [])
    
    msg_chainlit.content = ""
    await msg_chainlit.update()

    try:
        async for event in agente.astream_events({"messages": historico}, config, version="v2"):
            if event["event"] == "on_chat_model_stream" and event["data"]["chunk"].content:
                await msg_chainlit.stream_token(event["data"]["chunk"].content)
        
        historico.append(msg_chainlit.content)
        cl.user_session.set("historico_mensagens", historico)
        await msg_chainlit.update()

    except Exception as e:
        erro_str = str(e)
        
        # 💡 O PULO DO GATO 4: Dump de diagnóstico no caso de Payload Too Large
        if "too large" in erro_str.lower():
            print("\n" + "="*60)
            print("🚨 INTERCEPTAÇÃO DE ERRO: PAYLOAD TOO LARGE 🚨")
            print("="*60)
            print(f"-> QUANTIDADE DE MENSAGENS NO HISTÓRICO: {len(historico)}\n")
            print("-> ÚLTIMAS MENSAGENS ENVIADAS:")
            for i, msg in enumerate(historico[-3:]): 
                conteudo = msg.content if hasattr(msg, 'content') else str(msg)
                print(f"   [{i}] {type(msg).__name__}: {conteudo[:150]}...")
            
            print("\n-> SCHEMA DAS FERRAMENTAS ESCOLHIDAS PELO ROUTER:")
            print(json.dumps(ferramentas_dump, indent=2, ensure_ascii=False))
            print("="*60 + "\n")

        await cl.Message(content=f"❌ Erro: {erro_str}").send()

@cl.on_chat_end
async def on_chat_end():
    stack = cl.user_session.get("exit_stack")
    if stack: await stack.aclose()