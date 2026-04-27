# Publicar o Projeto para Testes

Este projeto pode ser distribuido de 2 formas simples:

## Opcao 1 (recomendada): Link online (Render)

### 1. Subir para GitHub
- Crie um repositorio com este projeto
- Envie os arquivos incluindo `requirements.txt` e `render.yaml`

### 2. Deploy no Render
- Acesse https://render.com
- Clique em New + > Blueprint
- Conecte ao repositorio
- O Render vai ler `render.yaml` automaticamente

### 3. Configurar chaves
No painel do servico, em Environment:
- `GOOGLE_API_KEY` (opcional)
- `OPENAI_API_KEY` (opcional)

### 4. Compartilhar
- Depois do deploy, copie a URL publica
- Envie para seus colegas

---

## Opcao 2: Executavel para Windows (.exe)

Importante: o `.exe` deve ser gerado em uma maquina Windows.

### 1. No Windows, dentro da pasta do projeto
```bash
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
```

### 2. Gerar executavel
```bash
pyinstaller --onefile --name mapa-poli main.py
```

### 3. Resultado
- O arquivo ficara em `dist\mapa-poli.exe`
- Compartilhe esse `.exe` com seus colegas

Observacao: esse executavel inicia a API. Para abrir o mapa automaticamente no navegador, voce pode criar um `.bat` para iniciar e abrir `http://127.0.0.1:9000`.

---

## Rodar localmente sem instalacao complexa (colegas dev)

Se o colega tiver Python:

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
uvicorn avatar:app --host 127.0.0.1 --port 9000
```

Abrir no navegador:
- http://127.0.0.1:9000
