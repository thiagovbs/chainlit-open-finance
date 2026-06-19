# 🏦 Agente Open Finance
Este projeto é um assistente de IA para consultas financeiras utilizando LangGraph, Chainlit e o protocolo MCP.

## 🚀 Como Rodar

1. Clone o repositório.
2. Crie um arquivo `.env` baseado nas variáveis do sistema (Sensedia Gateway e Backend com APIs OpenFinance).
3. Execute via Docker:
   ```bash
   docker build -t openfinance-bot .
   docker run -p 8000:8000 openfinance-bot
