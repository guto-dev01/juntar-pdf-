# Juntar PDFs

Envie PDFs, fotos e documentos pelo navegador, de qualquer tamanho (testado com 11 GB), e baixe tudo em um único PDF.

## O que dá para enviar

| Tipo | Formatos | Como vira PDF |
|---|---|---|
| PDF | `.pdf` | usado como está |
| Fotos e imagens | `.jpg`, `.png`, `.heic`, `.webp`, `.gif`, `.bmp`, `.tif`… | uma página A4 por imagem (o TIFF e o GIF com várias imagens viram várias páginas) |
| Word e texto | `.docx`, `.doc`, `.odt`, `.rtf`, `.txt`, `.md`, `.html` | pelo LibreOffice |
| Excel | `.xlsx`, `.xls`, `.ods`, `.csv` | pelo LibreOffice |
| PowerPoint | `.pptx`, `.ppt`, `.odp` | pelo LibreOffice, um slide por página |

A conversão acontece no envio, e cada arquivo vira PDF antes de entrar na fila. As fotos entram na orientação certa (a rotação da câmera é respeitada) e o fundo transparente vira branco. Para os documentos é preciso ter o LibreOffice instalado (`sudo apt install libreoffice`); sem ele, PDFs e imagens continuam funcionando.

## Como usar

```bash
./iniciar.sh
```

Abra http://localhost:8000, arraste os PDFs, ajuste a ordem e clique em **Juntar PDFs**.

- Os arquivos são enviados em partes de 32 MB. Se a conexão cair, o envio continua de onde parou. Se você recarregar a página e escolher o mesmo arquivo de novo, o envio também é retomado.
- O PDF final ganha um marcador para cada arquivo de origem.
- Em **Tamanho do arquivo final** dá para escolher:
  - **Qualidade original** (padrão): nada é recomprimido com perda. As páginas saem idênticas.
  - **Arquivo menor**: as imagens são recomprimidas em JPEG e as maiores que 1800 pixels são reduzidas. Texto, desenhos, imagens de 1 bit (escaneado em preto e branco) e imagens que não ficariam menores não são tocados. Em testes com documentos escaneados, o arquivo ficou cerca de 5 vezes menor, a uma velocidade de cerca de 700 MB por minuto.
- PDFs com senha de abertura são recusados. PDFs que só têm restrição de edição são aceitos, e o PDF final sai sem restrições.
- Arquivos de tipos não aceitos (vídeo, áudio, zip…) são ignorados na hora de escolher.
- Os marcadores e formulários preenchíveis dos PDFs originais não são mantidos.
- Os arquivos ficam na pasta `dados/` e são apagados automaticamente 24 horas depois. O botão **Limpar tudo** apaga na hora.
- Espaço em disco necessário: cerca de 2 vezes o tamanho total dos PDFs (os originais enviados mais o PDF final).

## Usar fora deste computador

```bash
./compartilhar.sh
```

Sobe um servidor separado (porta 8001) e um túnel gratuito da Cloudflare, e mostra um link como
`https://nome-aleatorio.trycloudflare.com/?chave=...`. Quem abre esse link completo entra direto. Quem tiver só o endereço, sem a chave, é bloqueado.

- O link muda toda vez que o script é iniciado e para de funcionar quando o script é encerrado (Ctrl+C) ou o computador desliga.
- Se o script estiver rodando em segundo plano, encerre com: `kill $(cat dados/compartilhar.pid)`
- Para carregar uma atualização do sistema sem trocar o link: `kill $(cat dados/servidor.pid)` (o servidor sobe de novo sozinho)
- A velocidade de envio para quem usa de fora depende da internet deste computador.

## Configuração (variáveis de ambiente)

| Variável | Padrão | Para que serve |
|---|---|---|
| `HOST` | `127.0.0.1` | Use `0.0.0.0` para acessar de outros computadores da rede |
| `PORT` | `8000` | Porta do servidor |
| `PDF_DATA_DIR` | `./dados` | Onde guardar envios e resultados (use um disco com bastante espaço) |
| `PDF_CHUNK_MB` | `32` | Tamanho de cada parte do envio |
| `PDF_RETENCAO_HORAS` | `24` | Depois de quanto tempo sem uso os arquivos são apagados |
| `PDF_CHAVE_ACESSO` | (nenhuma) | Exige essa chave para entrar; o `compartilhar.sh` gera uma sozinho |

Exemplo: `HOST=0.0.0.0 PDF_DATA_DIR=/mnt/hd-grande/pdfs ./iniciar.sh`

Sem `PDF_CHAVE_ACESSO`, qualquer pessoa que alcance o endereço pode usar. Por isso, para abrir na rede, defina uma chave.
Se usar atrás de um proxy (nginx), libere pelo menos 64 MB por requisição (`client_max_body_size 64m;`).
