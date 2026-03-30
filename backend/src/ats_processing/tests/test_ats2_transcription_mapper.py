"""Tests for ATS2TranscriptionMapper."""

from __future__ import annotations

import pytest

from ..application.ats2_transcription_mapper import ATS2TranscriptionMapper, ATS2Word


@pytest.fixture
def mapper() -> ATS2TranscriptionMapper:
    """Fixture для маппера."""
    return ATS2TranscriptionMapper()


def test_mapper_converts_words_to_dialogue(mapper: ATS2TranscriptionMapper) -> None:
    """Тест: маппер преобразует массив слов в читаемый диалог."""
    words = [
        ATS2Word(channel="A", startTime=0.0, endTime=0.5, word="Добрый"),
        ATS2Word(channel="A", startTime=0.5, endTime=1.0, word="день"),
        ATS2Word(channel="B", startTime=1.5, endTime=2.0, word="Здравствуйте"),
        ATS2Word(channel="B", startTime=2.0, endTime=2.5, word="мне"),
        ATS2Word(channel="B", startTime=2.5, endTime=3.0, word="нужна"),
        ATS2Word(channel="B", startTime=3.0, endTime=3.5, word="справка"),
    ]

    result = mapper.map_to_dialogue(words)

    assert "[Оператор]: Добрый день" in result
    assert "[Клиент]: Здравствуйте мне нужна справка" in result
    assert result.count("\n") == 1  # две реплики


def test_mapper_separates_channels(mapper: ATS2TranscriptionMapper) -> None:
    """Тест: маппер правильно разделяет каналы A и B."""
    words = [
        ATS2Word(channel="A", startTime=0.0, endTime=0.5, word="Алло"),
        ATS2Word(channel="B", startTime=0.5, endTime=1.0, word="Да"),
        ATS2Word(channel="A", startTime=1.0, endTime=1.5, word="Слушаю"),
        ATS2Word(channel="B", startTime=1.5, endTime=2.0, word="Вас"),
    ]

    result = mapper.map_to_dialogue(words)

    lines = result.split("\n")
    assert len(lines) == 4
    assert lines[0] == "[Оператор]: Алло"
    assert lines[1] == "[Клиент]: Да"
    assert lines[2] == "[Оператор]: Слушаю"
    assert lines[3] == "[Клиент]: Вас"


def test_mapper_handles_empty_transcription(mapper: ATS2TranscriptionMapper) -> None:
    """Тест: маппер корректно обрабатывает пустой массив."""
    result = mapper.map_to_dialogue([])
    assert result == ""


def test_mapper_handles_overlapping_timestamps(mapper: ATS2TranscriptionMapper) -> None:
    """Тест: маппер обрабатывает overlapping речи (оба говорят одновременно)."""
    words = [
        ATS2Word(channel="A", startTime=0.0, endTime=1.0, word="Говорю"),
        ATS2Word(channel="B", startTime=0.5, endTime=1.5, word="Перебиваю"),
        ATS2Word(channel="A", startTime=1.0, endTime=2.0, word="продолжаю"),
        ATS2Word(channel="B", startTime=1.5, endTime=2.5, word="тоже"),
    ]

    result = mapper.map_to_dialogue(words)

    # Проверяем, что маппер обработал overlapping (отсортировал по startTime)
    assert "[Оператор]: Говорю" in result
    assert "[Клиент]: Перебиваю" in result
    assert "[Оператор]: продолжаю" in result
    assert "[Клиент]: тоже" in result


def test_mapper_handles_long_pause(mapper: ATS2TranscriptionMapper) -> None:
    """Тест: маппер создаёт новую реплику при длинной паузе."""
    words = [
        ATS2Word(channel="A", startTime=0.0, endTime=0.5, word="Первая"),
        ATS2Word(channel="A", startTime=0.5, endTime=1.0, word="фраза"),
        # Пауза > 2 секунд
        ATS2Word(channel="A", startTime=4.0, endTime=4.5, word="Вторая"),
        ATS2Word(channel="A", startTime=4.5, endTime=5.0, word="фраза"),
    ]

    result = mapper.map_to_dialogue(words)

    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[0] == "[Оператор]: Первая фраза"
    assert lines[1] == "[Оператор]: Вторая фраза"


def test_mapper_handles_empty_words(mapper: ATS2TranscriptionMapper) -> None:
    """Тест: маппер игнорирует пустые слова."""
    words = [
        ATS2Word(channel="A", startTime=0.0, endTime=0.5, word="Привет"),
        ATS2Word(channel="A", startTime=0.5, endTime=1.0, word=""),  # пустое слово
        ATS2Word(channel="A", startTime=1.0, endTime=1.5, word="как"),
        ATS2Word(channel="A", startTime=1.5, endTime=2.0, word="   "),  # только пробелы
        ATS2Word(channel="A", startTime=2.0, endTime=2.5, word="дела"),
    ]

    result = mapper.map_to_dialogue(words)

    assert result == "[Оператор]: Привет как дела"
