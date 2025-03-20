import sys
import numpy as np
import torch
import sounddevice as sd
import whisper
import librosa
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTextEdit, QHBoxLayout,
    QVBoxLayout, QWidget, QLabel
)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QPalette, QColor, QTextCursor
import queue
import openai
import threading

# -------------------------------------
# ПАРАМЕТРЫ
# -------------------------------------
DEVICE_INDEX = 2        # Замените на индекс устройства с входными каналами
BLOCK_SIZE = 4096
TARGET_SR = 16000       # Whisper предпочтительно 16 kHz
ACCUMULATE_SECONDS = 5  # Увеличиваем время накопления до 5 секунд
AUDIO_QUEUE = queue.Queue()

# Проверка CUDA
print("CUDA доступна? ", torch.cuda.is_available())
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(torch.cuda.current_device()))

# Загрузка модели Whisper
model = whisper.load_model("large-v3-turbo", device="cuda")

# OpenAI API: создаем клиент
client = openai.OpenAI(api_key="sk-")

# Узнаём частоту дискретизации устройства (обычно 44100 или 48000)
device_info = sd.query_devices(DEVICE_INDEX, 'input')
device_sr = int(device_info['default_samplerate'])
print(f"Частота дискретизации устройства: {device_sr} Гц")

# Сколько сэмплов = ACCUMULATE_SECONDS * TARGET_SR
accumulate_samples = int(ACCUMULATE_SECONDS * TARGET_SR)


class AudioThread(QThread):
    """
    Отдельный поток:
    - считываем блок из sounddevice (в колбэке),
    - приводим к float32,
    - ресемплируем до 16 kHz,
    - аккумулируем ~5 секунд,
    - распознаём через Whisper,
    - если тишина (RMS ниже порога), не отправляем текст.
    """
    text_signal = pyqtSignal(str)

    # Порог RMS для определения «тишины»
    SILENCE_THRESHOLD = 0.003
    SILENCE_PAUSE_COUNT = 10  # Количество итераций тишины перед обработкой

    def __init__(self):
        super().__init__()
        self.running = True
        self.silence_counter = 0  # Счетчик тишины

    def run(self):
        audio_buffer = np.array([], dtype=np.float32)

        def audio_callback(indata, frames, time, status):
            if status:
                print("Audio status:", status)
            mono_data = indata[:, 0] if indata.ndim > 1 else indata
            AUDIO_QUEUE.put(mono_data.copy())

        with sd.InputStream(
            device=DEVICE_INDEX,
            samplerate=device_sr,
            channels=1,
            blocksize=BLOCK_SIZE,
            callback=audio_callback
        ):
            while self.running:
                if not AUDIO_QUEUE.empty():
                    chunk = AUDIO_QUEUE.get()

                    if chunk.dtype != np.float32:
                        chunk_float = chunk.astype(np.float32) / 32768.0
                    else:
                        chunk_float = chunk

                    if device_sr != TARGET_SR:
                        chunk_float = librosa.resample(
                            chunk_float,
                            orig_sr=device_sr,
                            target_sr=TARGET_SR
                        )
                        chunk_float = chunk_float.astype(np.float32)

                    audio_buffer = np.concatenate((audio_buffer, chunk_float))

                    rms = np.sqrt(np.mean(chunk_float**2))
                    if rms < self.SILENCE_THRESHOLD:
                        self.silence_counter += 1
                    else:
                        self.silence_counter = 0

                    if self.silence_counter > self.SILENCE_PAUSE_COUNT or audio_buffer.size >= accumulate_samples:
                        if audio_buffer.size > 0:
                            result = model.transcribe(
                                audio_buffer,
                                language="ru",
                                fp16=True,
                                beam_size=5,
                                best_of=5
                            )
                            text = result["text"].strip()
                            # Игнорируем "Продолжение следует..." и варианты фраз про DimaTorzok
                            if text and "продолжение следует" not in text.lower() and "субтитры делал dimatorzok" not in text.lower() and "добавил субтитры dimatorzok" not in text.lower():
                                # Дополнительная очистка текста от фраз, если они встретятся в другом месте
                                text = text.replace("Субтитры делал DimaTorzok", "").replace("субтитры делал DimaTorzok", "")
                                text = text.replace("Субтитры cделал DimaTorzok", "").replace("субтитры делал DimaTorzok", "")
                                text = text.replace("Добавил субтитры DimaTorzok", "").replace("добавил субтитры DimaTorzok", "")
                                text = text.strip()
                                
                                # Проверка, что текст не пустой после очистки
                                if text:
                                    self.text_signal.emit(text)
                                    
                            audio_buffer = np.array([], dtype=np.float32)
                            self.silence_counter = 0

                self.msleep(10)

    def stop(self):
        self.running = False


class MainWindow(QMainWindow):
    """
    Главное окно:
    - Слева: непрерывное поле для распознанного текста
    - Справа: ответ от ChatGPT, когда выделяем текст слева
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Whisper + GPT: Два поля (распознанный текст и ответ)")
        self.setGeometry(300, 200, 1200, 600)

        self.set_dark_theme()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)

        # Левая часть
        left_layout = QVBoxLayout()
        main_layout.addLayout(left_layout, stretch=2)

        label_left = QLabel("Распознанный текст:")
        label_left.setStyleSheet("font-size: 16px; font-weight: bold; color: #FFFFFF;")
        left_layout.addWidget(label_left)

        self.recognized_text_edit = QTextEdit()
        self.recognized_text_edit.setReadOnly(False)
        self.recognized_text_edit.setStyleSheet("""
            QTextEdit {
                font-size: 14px;
                background-color: #2E2E2E;
                color: #FFFFFF;
                border: 1px solid #444444;
                padding: 8px;
            }
        """)
        left_layout.addWidget(self.recognized_text_edit)

        # Правая часть
        right_layout = QVBoxLayout()
        main_layout.addLayout(right_layout, stretch=1)

        label_right = QLabel("Ответ GPT:")
        label_right.setStyleSheet("font-size: 16px; font-weight: bold; color: #FFFFFF;")
        right_layout.addWidget(label_right)

        self.gpt_text_edit = QTextEdit()
        self.gpt_text_edit.setReadOnly(True)
        self.gpt_text_edit.setStyleSheet("""
            QTextEdit {
                font-size: 16px; /* Увеличенный шрифт */
                background-color: #2E2E2E;
                color: #00FF00; /* Вернули зеленый цвет для ответов GPT */
                border: 1px solid #444444;
                padding: 8px;
            }
        """)
        right_layout.addWidget(self.gpt_text_edit)

        # Поток аудио
        self.audio_thread = AudioThread()
        self.audio_thread.text_signal.connect(self.append_recognized_text_safely)
        self.audio_thread.start()

        # Ловим отпускание мыши в левом поле
        self.recognized_text_edit.mouseReleaseEvent = self.on_mouse_release_in_recognized

    def set_dark_theme(self):
        """Простая тёмная тема PyQt."""
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(45, 45, 45))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
        palette.setColor(QPalette.ToolTipBase, Qt.white)
        palette.setColor(QPalette.ToolTipText, Qt.white)
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(70, 70, 70))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Highlight, QColor(142, 45, 197))
        palette.setColor(QPalette.HighlightedText, Qt.black)
        self.setPalette(palette)

        self.setStyleSheet("""
            QMainWindow {
                background-color: #2D2D2D;
            }
            QLabel {
                color: #FFFFFF;
            }
        """)

    def append_recognized_text_safely(self, new_text: str):
        """
        Добавляем распознанный текст в левое поле,
        не сбрасывая выделение, если пользователь что-то выделил.
        """
        old_cursor = self.recognized_text_edit.textCursor()
        had_selection = old_cursor.hasSelection()

        end_cursor = self.recognized_text_edit.textCursor()
        end_cursor.movePosition(QTextCursor.End)
        self.recognized_text_edit.setTextCursor(end_cursor)

        self.recognized_text_edit.insertPlainText(" " + new_text)

        if had_selection:
            self.recognized_text_edit.setTextCursor(old_cursor)
        else:
            self.recognized_text_edit.moveCursor(QTextCursor.End)

    def on_mouse_release_in_recognized(self, event):
        """
        Когда пользователь отпускает мышь в левом поле:
        проверяем, есть ли выделенный текст -> отправляем его в GPT.
        """
        super(QTextEdit, self.recognized_text_edit).mouseReleaseEvent(event)

        cursor = self.recognized_text_edit.textCursor()
        selected_text = cursor.selectedText().strip()
        if selected_text:
            threading.Thread(target=self.call_openai_api, args=(selected_text,)).start()

    def call_openai_api(self, text: str):
        """
        Вызов ChatCompletion (GPT) для выделенного текста.
        """
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",  # Или "gpt-4", если есть доступ
                messages=[
                    {"role": "system", "content": "Проводится собеседование на должность ----, ты должен отвечать на вопросы кратко и понятно. На ---- языке. Ты не должен задават ьвопросы, только отвечать на них"},
                    {"role": "user", "content": f"Обработай следующий текст, он может содержать ошибки, игнорируй их, или додумай ответ: {text}"}
                ],
                max_tokens=1000,
                temperature=0.5
            )
            result = response.choices[0].message.content.strip()
            self.append_gpt_text(f"[User selected]: {text}\n[GPT]: {result}\n")
        except Exception as e:
            self.append_gpt_text(f"[Ошибка]: {e}\n")

    def append_gpt_text(self, text: str):
        """Добавить текст в правое поле (ответ GPT) с автопрокруткой."""
        self.gpt_text_edit.append(text)
        self.gpt_text_edit.moveCursor(QTextCursor.End)  # Автопрокрутка

    def closeEvent(self, event):
        """Закрываем поток аудио при закрытии окна."""
        self.audio_thread.stop()
        self.audio_thread.wait()
        event.accept()


if __name__ == "__main__":
    print("Все устройства:\n", sd.query_devices())

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())