import os
from rembg import remove
from PIL import Image

# Cria uma subpasta para salvar os frames limpos sem sobrescrever os originais
pasta_saida = "transparentes"
os.makedirs(pasta_saida, exist_ok=True)

print("Iniciando a remoção de fundo... Isso pode levar alguns minutinhos.")

# Percorre todos os arquivos na pasta atual
for arquivo in os.listdir("."):
    # Filtra apenas os arquivos PNG que começam com "frame_"
    if arquivo.startswith("frame_") and arquivo.endswith(".png"):
        caminho_entrada = arquivo
        caminho_saida = os.path.join(pasta_saida, arquivo)
        
        print(f"Recortando: {arquivo} ...")
        
        try:
            # Abre a imagem original
            imagem_original = Image.open(caminho_entrada)
            
            # Remove o fundo (mágica da IA acontece aqui)
            imagem_sem_fundo = remove(imagem_original)
            
            # Salva a imagem com fundo transparente na nova pasta
            imagem_sem_fundo.save(caminho_saida)
        except Exception as e:
            print(f"Erro ao processar {arquivo}: {e}")

print(f"\nPronto! Todos os frames com fundo transparente estão na pasta '{pasta_saida}'.")