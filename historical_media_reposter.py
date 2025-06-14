import asyncio
import json
from datetime import date
from telethon import TelegramClient, events, errors
import os
import time
from tqdm import tqdm # Usaremos a tqdm síncrona para os callbacks

# ------------------------------------------------------------------------------------
# CONFIGURAÇÃO 
# ------------------------------------------------------------------------------------
API_ID = 0000000
API_HASH = 'sua api hash aqui'
BOT_TOKEN = 'bot token aqui'

# Os dados acima são obtidos atrvés do site my.telegram.org, faça login e vá a "API development tools.
# O bot token é obtido através do @botFather no telegram.

SOURCE_CHANNEL_ID = -111111111  # Canal que você irá pegar as mídias.

DESTINATION_CHANNEL_ID_1 = -1111111111 # id do canal 1.
CUSTOM_CAPTION_1 = """
**Mídia Exclusiva - Canal VIP** 🚀
"""
DAILY_LIMIT_CHANNEL_1 = 10 # quantidade de mídias do canal 1.

DESTINATION_CHANNEL_ID_2 = -11111111 # id do canal 2.
CUSTOM_CAPTION_2 = """
Legenda do canal 2 aqui.
"""
DAILY_LIMIT_CHANNEL_2 = 10 # quantidade de mídias do canal 2.

STATE_FILE = "historical_reposter_state.json"
POST_INTERVAL_SECONDS = 60 # tempo entre uma postagem e outra.
MAX_FILE_SIZE_MB = 100 # tamanho máximo das mídias baixadas.  
# ------------------------------------------------------------------------------------
# FIM DA CONFIGURAÇÃO
# ------------------------------------------------------------------------------------

user_client = TelegramClient('user_historical_session', API_ID, API_HASH)
bot_client = TelegramClient('bot_historical_session', API_ID, API_HASH)

# Funções load_state e save_state permanecem iguais
def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            state.setdefault('last_processed_message_id', 0)
            state.setdefault('date_of_last_posts', date.min.isoformat())
            state.setdefault('posts_today_channel1', 0)
            state.setdefault('posts_today_channel2', 0)
            return state
    except FileNotFoundError:
        return {'last_processed_message_id': 0, 'date_of_last_posts': date.min.isoformat(), 'posts_today_channel1': 0, 'posts_today_channel2': 0}
    except json.JSONDecodeError:
        print(f"Erro: Arquivo de estado '{STATE_FILE}' corrompido. Iniciando com estado padrão.")
        return {'last_processed_message_id': 0, 'date_of_last_posts': date.min.isoformat(), 'posts_today_channel1': 0, 'posts_today_channel2': 0}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)
    # Comentado para reduzir verbosidade, a barra de progresso já indica atividade
    # print(f"Estado salvo: {state}") 

# --- Função de callback para tqdm ---
# Usaremos uma função auxiliar para atualizar a barra de progresso do tqdm
# Esta função será chamada pelo Telethon durante o download/upload.
_current_pbar = None

def _tqdm_progress_callback(current_bytes, total_bytes):
    global _current_pbar
    if _current_pbar is None: # Primeira chamada para este arquivo
        # Se total_bytes for 0 ou None, tqdm mostrará um spinner e bytes/s
        _current_pbar = tqdm(total=total_bytes, unit='B', unit_scale=True, desc="Transferindo", leave=False) 
    
    _current_pbar.update(current_bytes - _current_pbar.n) # Atualiza pelo delta
    
    if current_bytes >= total_bytes: # Transferência concluída
        _current_pbar.close()
        _current_pbar = None

async def process_historical_media():
    global _current_pbar # Para que o callback possa resetá-lo
    print(">>> Iniciando processamento de mídias históricas...")
    state = load_state()
    today_iso = date.today().isoformat()

    if state['date_of_last_posts'] != today_iso:
        print(f"Novo dia ({today_iso}). Resetando contagens diárias.")
        state['posts_today_channel1'] = 0
        state['posts_today_channel2'] = 0
        state['date_of_last_posts'] = today_iso
        save_state(state)

    try:
        source_entity = await user_client.get_entity(SOURCE_CHANNEL_ID)
        print(f"Canal de origem '{getattr(source_entity, 'title', SOURCE_CHANNEL_ID)}' carregado.")
    except Exception as e:
        print(f"Erro fatal ao carregar canal de origem '{SOURCE_CHANNEL_ID}': {e}.")
        return

    processed_in_this_run = 0
    posted_to_ch1_this_run = 0
    posted_to_ch2_this_run = 0

    # --- REMOVIDO o tqdm do loop principal de user_client.iter_messages ---
    async for message in user_client.iter_messages(
            source_entity,
            limit=None,
            reverse=True,
            min_id=state['last_processed_message_id']
        ):
        
        current_message_id = message.id
        print(f"\nAnalisando mensagem ID: {current_message_id} do canal de origem...")

        if not message.media:
            print(f"Mensagem {current_message_id} não contém mídia. Pulando.")
            state['last_processed_message_id'] = current_message_id
            save_state(state) # Salva o avanço mesmo pulando
            processed_in_this_run +=1
            continue

        file_size_bytes_metadata = None
        if message.file and hasattr(message.file, 'size') and message.file.size is not None:
            file_size_bytes_metadata = message.file.size
        
        if file_size_bytes_metadata is not None and file_size_bytes_metadata > 0:
            file_size_mb_estimate = file_size_bytes_metadata / (1024 * 1024)
            print(f"Mídia detectada (Msg ID {current_message_id}). Tamanho estimado: {file_size_mb_estimate:.2f} MB.")
            if file_size_mb_estimate > MAX_FILE_SIZE_MB:
                print(f"❌ Estimativa ({file_size_mb_estimate:.2f} MB) > {MAX_FILE_SIZE_MB} MB. NÃO SERÁ BAIXADA.")
                state['last_processed_message_id'] = current_message_id
                save_state(state)
                processed_in_this_run += 1
                continue 
        else:
            print(f"[AVISO] Tam. não determinado antes do download para Msg ID {current_message_id}. Download será tentado.")

        can_post_to_channel1 = state['posts_today_channel1'] < DAILY_LIMIT_CHANNEL_1
        can_post_to_channel2 = state['posts_today_channel2'] < DAILY_LIMIT_CHANNEL_2

        if not can_post_to_channel1 and not can_post_to_channel2:
            print(f"Limite diário atingido para ambos os canais hoje ({today_iso}). Parando.")
            break 

        downloaded_media_path = None
        media_successfully_downloaded = False
        actual_file_size_mb = 0 
        actual_file_size_bytes = 0

        try:
            print(f"Tentando baixar mídia da mensagem {current_message_id}...")
            _current_pbar = None # Reseta a barra de progresso global para o download
            time_inicio_download = time.time()
            try:
                if message.media:
                    # Prepara o callback para o tqdm de download
                    # Se file_size_bytes_metadata não for conhecido, a barra será um spinner
                    total_size_for_download_bar = file_size_bytes_metadata if file_size_bytes_metadata else 0

                    # Cria a barra de progresso para o download
                    # Usaremos um truque para reinicializar _current_pbar dentro do callback se total_size_for_download_bar for 0
                    # ou podemos simplesmente não passar o total para o tqdm se for 0.
                    
                    _current_pbar = tqdm(total=total_size_for_download_bar if total_size_for_download_bar > 0 else None, 
                                         unit='B', unit_scale=True, desc=f'Baixando ID {current_message_id}', leave=False)

                    downloaded_media_path = await user_client.download_media(
                        message.media, 
                        file="temp_downloaded_media",
                        progress_callback=_tqdm_progress_callback # Usa o callback que atualiza _current_pbar
                    )
                    
                    if _current_pbar is not None and _current_pbar.total is not None and _current_pbar.n < _current_pbar.total : # Garante que a barra feche se o callback não for chamado no final
                         _current_pbar.update(_current_pbar.total - _current_pbar.n) # Força a completar
                         _current_pbar.close()
                         _current_pbar = None

                    time_fim_download = time.time()
                
                    if downloaded_media_path and os.path.exists(downloaded_media_path):
                        actual_file_size_bytes = os.path.getsize(downloaded_media_path)
                        actual_file_size_mb = actual_file_size_bytes / (1024 * 1024)
                        tempo_download_seg = time_fim_download - time_inicio_download
                        print(f"Mídia baixada: {downloaded_media_path} ({actual_file_size_mb:.2f} MB) em {tempo_download_seg:.2f} segundos.")
                        media_successfully_downloaded = True
                    else:
                        print(f"Falha ao baixar mídia da mensagem {current_message_id}.")
            except errors.MediaEmptyError: print(f" ID {current_message_id}: Erro MediaEmptyError ao baixar.")
            except Exception as e_download: print(f" ID {current_message_id}: Erro crítico ao baixar: {type(e_download).__name__} - {e_download}")
            finally: # Garante que a barra de progresso seja fechada se ainda estiver ativa
                if _current_pbar: _current_pbar.close(); _current_pbar = None


            if not media_successfully_downloaded:
                state['last_processed_message_id'] = current_message_id; save_state(state); processed_in_this_run +=1
                if downloaded_media_path and os.path.exists(downloaded_media_path): os.remove(downloaded_media_path)
                continue

            if actual_file_size_mb > MAX_FILE_SIZE_MB:
                print(f" ID {current_message_id}: Tamanho real ({actual_file_size_mb:.2f}MB) > {MAX_FILE_SIZE_MB}MB. Pulando.")
                state['last_processed_message_id'] = current_message_id; save_state(state); processed_in_this_run +=1
                continue 
            
            # Postagem para Canal 1
            if can_post_to_channel1:
                print(f"Tentando postar mídia ({actual_file_size_mb:.2f} MB) no Canal 1...")
                _current_pbar = None # Reseta para o upload
                try:
                    with tqdm(total=actual_file_size_bytes, unit='B', unit_scale=True, desc=f'Enviando CH1 (ID {current_message_id})', leave=False) as pbar_ch1:
                        _current_pbar = pbar_ch1 # Permite que o callback global acesse esta barra específica
                        await bot_client.send_file(DESTINATION_CHANNEL_ID_1, file=downloaded_media_path, caption=CUSTOM_CAPTION_1, parse_mode='md', progress_callback=_tqdm_progress_callback)
                    state['posts_today_channel1'] += 1; posted_to_ch1_this_run +=1
                    print(f" ID {current_message_id}: ✅ Postada Canal 1 (Hoje: {state['posts_today_channel1']}/{DAILY_LIMIT_CHANNEL_1})")
                    await asyncio.sleep(POST_INTERVAL_SECONDS)
                except Exception as e_ch1: print(f" ID {current_message_id}: ❌ Erro Post Canal 1: {type(e_ch1).__name__}")
                finally: 
                    if _current_pbar: _current_pbar.close(); _current_pbar = None

            # Postagem para Canal 2
            if can_post_to_channel2 and state['posts_today_channel2'] < DAILY_LIMIT_CHANNEL_2:
                print(f"Tentando postar mídia ({actual_file_size_mb:.2f} MB) no Canal 2...")
                _current_pbar = None # Reseta para o upload
                try:
                    with tqdm(total=actual_file_size_bytes, unit='B', unit_scale=True, desc=f'Enviando CH2 (ID {current_message_id})', leave=False) as pbar_ch2:
                        _current_pbar = pbar_ch2
                        await bot_client.send_file(DESTINATION_CHANNEL_ID_2, file=downloaded_media_path, caption=CUSTOM_CAPTION_2, parse_mode='md', progress_callback=_tqdm_progress_callback)
                    state['posts_today_channel2'] += 1; posted_to_ch2_this_run += 1
                    print(f" ID {current_message_id}: ✅ Postada Canal 2 (Hoje: {state['posts_today_channel2']}/{DAILY_LIMIT_CHANNEL_2})")
                    await asyncio.sleep(POST_INTERVAL_SECONDS)
                except Exception as e_ch2: print(f" ID {current_message_id}: ❌ Erro Post Canal 2: {type(e_ch2).__name__}")
                finally:
                    if _current_pbar: _current_pbar.close(); _current_pbar = None
        
        except Exception as loop_error: print(f" ID {current_message_id}: Erro inesperado no loop: {loop_error}")
        finally:
            if downloaded_media_path and os.path.exists(downloaded_media_path):
                try: os.remove(downloaded_media_path); print(f" ID {current_message_id}: Temp removido.")
                except Exception as e_remove: print(f"Erro ao remover temp '{downloaded_media_path}': {e_remove}")
        
        state['last_processed_message_id'] = current_message_id
        save_state(state)
        processed_in_this_run += 1

    # ... (prints de sumário e função main como antes) ...
    print(f"\n--- Fim do processamento para esta execução ---")
    print(f"Total de mensagens do canal de origem analisadas nesta execução: {processed_in_this_run}")
    print(f"Total de postagens no Canal 1 nesta execução: {posted_to_ch1_this_run}")
    print(f"Total de postagens no Canal 2 nesta execução: {posted_to_ch2_this_run}")
    print(f"Contagem total de hoje para Canal 1: {state['posts_today_channel1']}/{DAILY_LIMIT_CHANNEL_1}")
    print(f"Contagem total de hoje para Canal 2: {state['posts_today_channel2']}/{DAILY_LIMIT_CHANNEL_2}")

async def main():
    global user_client, bot_client
    try:
        await bot_client.start(bot_token=BOT_TOKEN)
        print("Bot cliente (para postar) conectado.")
        await user_client.start()
        print("Cliente de usuário (para ler) conectado.")
        await process_historical_media()
    except Exception as e_main:
        print(f"Erro fatal na execução principal: {e_main}")
    finally:
        print("Desconectando clientes...")
        if user_client.is_connected() and not user_client.is_bot(): 
            await user_client.disconnect()
        if bot_client.is_connected(): 
            await bot_client.disconnect()
        print("Clientes desconectados. Fim do script.")

if __name__ == '__main__':
    asyncio.run(main())