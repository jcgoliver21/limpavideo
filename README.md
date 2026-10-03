# LimpaVídeo Studio — editor de MP4 quadro a quadro

Aplicação para analisar textos com OCR, editar máscaras em quadros individuais, detectar textos repetidos e remover automaticamente, propagar uma remoção para ocorrências do mesmo texto e exportar um novo MP4 completo. O original não é sobrescrito. A reconstrução usa inpainting; não promete recuperar detalhes que não existem nos quadros restantes.

**Versão web (sem instalar):** [https://jcgoliver21.github.io/limpavideo/](https://jcgoliver21.github.io/limpavideo/)

O vídeo não é enviado a nenhum servidor. Chrome ou Edge exportam o MP4. A versão local abaixo continua melhor para vídeos grandes, OCR no vídeo inteiro e reconstrução temporal com OpenCV.

> Use somente vídeos próprios ou para os quais você tenha autorização para editar. Remover marcas pode infringir direitos autorais, termos de uso ou atribuição. Confirme que você tem autorização antes de editar ou publicar o resultado.

## Executar no Windows

### Gerar o executável `.exe`

Este projeto inclui `Gerar_EXE.bat`, um iniciador de dois cliques para gerar `dist\LimpaVideo.exe` em Windows x64. Extraia o ZIP pelo Explorador de Arquivos e abra `Gerar_EXE.bat`. Não é preciso abrir o PowerShell nem digitar comandos. O primeiro build exige Python 3.10+, FFmpeg e Tesseract OCR com os dados `por` e `eng` disponíveis no `PATH`; o próprio iniciador instala as dependências Python e empacota esses componentes.

Depois do build, dê dois cliques em `dist\LimpaVideo.exe`. Ele abre o navegador e escuta somente em `127.0.0.1`. O computador de destino não precisa de Python, FFmpeg ou Tesseract para executar o EXE já compilado.

### Executar pelo código-fonte

Instale Python 3.10+, FFmpeg (`ffmpeg` e `ffprobe`) e Tesseract OCR com dados de idioma português e inglês. Se `py` não existir, use `python` no lugar. Na pasta do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:7860` e encerre o servidor com `Ctrl+C`.

## Executar no macOS ou Linux

Instale Python 3.10+, FFmpeg e Tesseract OCR (`por` e `eng`); depois, na pasta do projeto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:7860`.

## Fluxo de edição

1. **Carregue o MP4.** A demonstração limita o upload a 700 MB.
2. **Analise textos.** Escolha a amostragem e clique em **Analisar e separar quadros**. Para legendas rápidas, diminua o intervalo. A análise gera quadros de prévia, caixas de texto e um ZIP com imagens e CSV.
3. **Abra um resultado.** Em um quadro OCR, clique em **Editar texto e criar máscara**. O editor abre o quadro exato e sugere uma máscara retangular com margem ajustável.
4. **Refine a máscara.** Use pincel, borracha, **Desfazer**, **Refazer** e navegação de um quadro por vez. **Expandir máscara** (0–20 px) cobre halos e bordas antialiasadas; **Raio** (1–25 px) controla a vizinhança usada pelo preenchimento. Para letras grandes, teste expansão de 4–8 px e raio de 8–15 px; reduza-os se a reconstrução invadir detalhes. As máscaras podem ser salvas individualmente em até 100 quadros por vídeo; a edição pendente é salva automaticamente ao navegar.
5. **Confira.** Clique em **Prévia deste quadro** para comparar o original com o inpainting antes da exportação. A prévia e o MP4 usam a mesma expansão e o mesmo raio.
6. **Escolha como aplicar e exporte o vídeo completo:**
   - **Propagar para texto OCR igual:** transfere a máscara ajustada para caixas do mesmo texto detectadas em outros tempos e acompanha deslocamentos/tamanhos. Só edita períodos em que a amostragem OCR reconheceu o texto.
   - **Máscara salva mais próxima no tempo:** usa, em cada quadro do vídeo, a máscara-chave editada mais próxima. É útil quando as posições mudam manualmente; máscaras podem ser aplicadas também em trechos sem texto.
   - **Máscara atual fixa:** repete a máscara atualmente exibida nas mesmas coordenadas em todos os quadros. É adequada para marca fixa; não exige salvar keyframes.

Clique em **Processar e exportar vídeo completo**. O resultado é um novo MP4 H.264 e preserva as faixas de áudio do original quando existentes. Baixe o arquivo pelo botão exibido ao final.

## Correções desta versão

- A exportação não falha mais em vídeo sem áudio nem em largura ou altura ímpar, e o erro do FFmpeg mostra a causa.
- A navegação rápida não mistura a máscara de um quadro com a de outro.
- Há retângulo, atalhos, arrastar o arquivo, trecho de exportação e aviso se FFmpeg ou Tesseract não estiverem instalados.

## Recursos incluídos

- Extração/amostragem de quadros e OCR em português/inglês.
- Sugestão de texto recorrente em posição semelhante como possível marca d’água — não é uma confirmação automática.
- Edição manual independente por quadro, máscaras-chave persistentes e lista para reabrir/remover edições.
- Caixa inicial de máscara a partir da detecção OCR, com margem configurável, expansão pós-máscara de até 20 px e raio de reconstrução de até 25 px.
- Modo automático que detecta textos repetidos na mesma posição e remove sem pintura manual (também disponível no app local após a análise OCR).
- Pincel, borracha, desfazer, refazer e navegação quadro a quadro.
- Prévia comparativa de um quadro com o método, expansão da máscara e raio escolhidos.
- Propagação geométrica da máscara para ocorrências OCR do mesmo texto, modo de máscara-chave mais próxima e modo fixo.
- Progresso de análise/processamento, exportação do MP4 completo e recuperação de sessões/status após recarregar a página ou reiniciar o servidor, quando a sessão ainda existir.
- Respostas de erro estruturadas em JSON na API; erros de sessão aparecem como orientação para reenviar o MP4, em vez de erro de interpretação `Unexpected token '<'`.

## Privacidade e armazenamento

Na versão local, o processamento acontece no computador em que o aplicativo está rodando. Na [versão web](https://jcgoliver21.github.io/limpavideo/), o MP4 também fica no navegador: só o motor de vídeo e o OCR são baixados de CDN, não o seu arquivo. Os originais nunca são sobrescritos. A página avisa se FFmpeg, ffprobe ou Tesseract não estiverem no PATH.

## Limitações importantes

- Somente MP4, até 700 MB; no máximo 100 quadros editados e 3.000 amostras OCR por análise.
- A remoção automática depende do OCR: pode falhar com texto pequeno, estilizado, baixo contraste, movimento rápido ou fora dos intervalos amostrados; não identifica logotipos sem texto de forma confiável. Revise sempre a prévia.
- A propagação por texto exige que o OCR reconheça o mesmo texto; falhas ou variações no reconhecimento podem deixar trechos sem edição. Revise as caixas detectadas e a prévia.
- O modo de keyframes mais próximos repete a máscara mais próxima inclusive quando a marca não está visível; use-o com cautela em texto temporário. O modo por texto limita a aplicação aos períodos amostrados em que a ocorrência foi detectada.
- O app usa inpainting clássico do OpenCV, não preenchimento generativo. Ele pode remover letras, mas não inventa com fidelidade partes complexas escondidas. Regiões grandes, rostos, objetos em movimento ou fundos detalhados podem continuar borrados mesmo com raio maior; quando possível, a melhor solução é reexportar a fonte sem a sobreposição ou usar uma ferramenta de inpainting generativo/remoção de objetos com suporte temporal.
- A taxa de quadros variável é normalizada para a taxa estimada do vídeo; legendas e alguns metadados adicionais não são preservados.
