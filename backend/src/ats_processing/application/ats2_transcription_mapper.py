"""ATS2 Transcription Mapper — преобразование пословной транскрипции в диалог."""

from __future__ import annotations

from pydantic import BaseModel


class ATS2Word(BaseModel):
    """Одно слово из пословной транскрипции ATS2."""

    channel: str  # "A" (оператор) или "B" (клиент)
    startTime: float  # время начала в секундах
    endTime: float  # время окончания в секундах
    word: str  # само слово


class ATS2TranscriptionMapper:
    """
    Преобразует пословную транскрипцию ATS2 в текстовый диалог.

    Формат выхода: "[Оператор]: текст...\\n[Клиент]: текст..."
    - channel A = Оператор
    - channel B = Клиент
    """

    PAUSE_THRESHOLD_SEC = 2.0  # Пауза больше 2 секунд = новая реплика
    OPERATOR_LABEL = "Оператор"
    CLIENT_LABEL = "Клиент"

    def map_to_dialogue(self, words: list[ATS2Word]) -> str:
        """
        Преобразовать массив слов в читаемый диалог.

        Args:
            words: список слов с метаданными (channel, startTime, endTime, word)

        Returns:
            Форматированный диалог вида "[Оператор]: текст\\n[Клиент]: текст"
        """
        if not words:
            return ""

        # Сортируем по времени начала
        sorted_words = sorted(words, key=lambda w: w.startTime)

        dialogue_lines: list[str] = []
        current_channel: str | None = None
        current_words: list[str] = []
        last_end_time: float = 0.0

        for word_obj in sorted_words:
            channel = word_obj.channel
            start_time = word_obj.startTime
            word = word_obj.word.strip()

            if not word:
                continue

            # Проверяем, нужно ли начать новую реплику
            is_new_speaker = channel != current_channel
            is_long_pause = (start_time - last_end_time) > self.PAUSE_THRESHOLD_SEC

            if is_new_speaker or (is_long_pause and current_words):
                # Сохраняем текущую реплику
                if current_words and current_channel is not None:
                    speaker = self._get_speaker_label(current_channel)
                    line = f"[{speaker}]: {' '.join(current_words)}"
                    dialogue_lines.append(line)
                    current_words = []

                current_channel = channel

            current_words.append(word)
            last_end_time = word_obj.endTime

        # Добавляем последнюю реплику
        if current_words and current_channel is not None:
            speaker = self._get_speaker_label(current_channel)
            line = f"[{speaker}]: {' '.join(current_words)}"
            dialogue_lines.append(line)

        return "\n".join(dialogue_lines)

    def _get_speaker_label(self, channel: str) -> str:
        """Получить метку спикера по каналу."""
        return self.OPERATOR_LABEL if channel.upper() == "A" else self.CLIENT_LABEL
