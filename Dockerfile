# 1. Usa uma imagem oficial do Python, versão slim (mais leve e segura)
FROM python:3.11-slim

# 2. Define o diretório de trabalho dentro do container
WORKDIR /app

# 3. Copia apenas o arquivo de requisitos primeiro (para otimizar o cache do Docker)
COPY requirements.txt .

# 4. Instala as dependências sem guardar lixo de cache
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copia o resto do código para dentro do container (o .dockerignore bloqueia o venv e o .env)
COPY . .

# 6. Expõe a porta padrão que o Chainlit usa
EXPOSE 8000

# 7. Comando para iniciar a aplicação (o host 0.0.0.0 é obrigatório no Docker para liberar o acesso)
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]