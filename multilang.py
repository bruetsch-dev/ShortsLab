"""Extra audio tracks, so one sketch explainer can be published in eight languages.

YouTube plays a multi-language video as ONE video with several audio tracks; the viewer's own
language is selected for them. This module produces those tracks: the script is translated by
the reasoning model, spoken by the same TTS stack the original uses, and written next to the
render so the pair can be uploaded together.

WHICH LANGUAGES. The seven below are the largest YouTube audiences after English THAT SEED
SPEECH CAN PRONOUNCE, and that second clause is a real constraint rather than a preference.
Seed's language list is zh, en, ja, es-mx, id, pt-br, ko, it, de, fr - it has no Hindi and no
Russian, which are otherwise top-five on YouTube. Sending Hindi text to a Spanish voice does not
produce Hindi; it produces Spanish phonetics reading Devanagari transliteration. So Hindi and
Russian are out and Korean is in, and each language is read by Seed's OWN native speaker for it
(felipe_es, martins_pt, han_id, minimi_ja, sven_de, usseau_fr, shane_ko) rather than by one
voice handed a language label.

The original language is never re-generated: the video already carries it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import agent_core
import pipeline

# code: the BCP-47-ish tag used in the filename and in YouTube's track picker.
# seed_voice / seed_language: ByteDance Seed Speech, which has a native speaker per language.
# gemini_language: the fallback engine's own language name, for what Seed cannot pronounce.
LANGUAGES = [
    {"code": "es", "name": "Spanish", "native": "Espanol",
     "seed_voice": "felipe_es", "seed_language": "es-mx",
     "gemini_language": "Spanish (Latin America)"},
    {"code": "pt", "name": "Portuguese (Brazil)", "native": "Portugues",
     "seed_voice": "martins_pt", "seed_language": "pt-br",
     "gemini_language": "Portuguese (Brazil)"},
    {"code": "ko", "name": "Korean", "native": "Hangugeo",
     # Stands in for Hindi, which is a bigger YouTube audience but which Seed cannot pronounce.
     "seed_voice": "shane_ko", "seed_language": "ko",
     "gemini_language": "Korean (South Korea)"},
    {"code": "id", "name": "Indonesian", "native": "Bahasa Indonesia",
     "seed_voice": "han_id", "seed_language": "id",
     "gemini_language": "Indonesian (Indonesia)"},
    {"code": "ja", "name": "Japanese", "native": "Nihongo",
     "seed_voice": "minimi_ja", "seed_language": "ja",
     "gemini_language": "Japanese (Japan)"},
    {"code": "de", "name": "German", "native": "Deutsch",
     "seed_voice": "sven_de", "seed_language": "de",
     "gemini_language": "German (Germany)"},
    {"code": "fr", "name": "French", "native": "Francais",
     "seed_voice": "usseau_fr", "seed_language": "fr",
     "gemini_language": "French (France)"},
]

DEFAULT_TRANSLATION_MODEL = "anthropic/claude-opus-4.8"
# One translation call per language. The reasoning model reads the whole script so a term
# introduced in the first minute is still translated the same way in the twentieth.
TRANSLATE_TIMEOUT_S = 900


class MultiLanguageError(RuntimeError):
    """Raised when a track cannot be produced. Never fatal to the video itself."""


def languages_for(source_code="en"):
    """The tracks worth producing for a video already spoken in `source_code`."""
    source = str(source_code or "en").strip().lower()[:2]
    return [lang for lang in LANGUAGES if lang["code"] != source]


def _translation_prompt(language):
    return (
        f"You translate video narration into {language['name']}.\n\n"
        "This is a spoken voiceover for an animated explainer, not a document. Translate it so "
        "it can be READ ALOUD by a narrator and sound like it was written in "
        f"{language['name']} to begin with.\n\n"
        "RULES\n"
        "1. Keep the meaning exactly. This is factual content - do not soften, embellish or "
        "invent claims, and do not add or remove any fact.\n"
        "2. Keep the paragraph breaks. One paragraph in, one paragraph out, in the same order. "
        "The timing of the finished video depends on this.\n"
        "3. Keep the register: plain, direct, second person, short sentences. Do not make it "
        "more formal than the original.\n"
        "4. Translate technical terms with the word a speaker of the language would actually "
        "use, and keep it consistent throughout.\n"
        "5. Numbers, units and proper nouns stay correct; localise number formatting.\n"
        "6. Output ONLY the translated narration. No preamble, no notes, no markdown fences, "
        "no bracketed stage directions."
    )


def translate_script(script, language, model=None, status_cb=None, timeout=TRANSLATE_TIMEOUT_S):
    """The narration in `language`, paragraph-for-paragraph. Raises on an unusable answer."""
    text = str(script or "").strip()
    if not text:
        raise MultiLanguageError("There is no script to translate.")
    model = str(model or DEFAULT_TRANSLATION_MODEL)
    if status_cb:
        status_cb(f"{language['name']}: translating {len(text.split())} words with {model}...")
    data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
        "model": model,
        "messages": [{"role": "system", "content": _translation_prompt(language)},
                     {"role": "user", "content": text}],
        "temperature": 0.3, "max_tokens": 32000,
    }, timeout=timeout)
    try:
        out = str(data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise MultiLanguageError(f"{language['name']}: the model returned no text.") from exc
    # A model that decides to explain itself first would put commentary into the voiceover.
    out = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", out).strip()
    if len(out.split()) < max(20, len(text.split()) // 6):
        raise MultiLanguageError(
            f"{language['name']}: the translation came back far too short "
            f"({len(out.split())} words for {len(text.split())}) - refusing to speak it.")
    if status_cb:
        status_cb(f"{language['name']}: translated ({len(out.split())} words).")
    return out


def voice_for(language, tts_model=None):
    """(voice, language_string, model) for this language.

    Always Seed Speech, always that language's own native speaker. The engine is not inherited
    from the original run on purpose: the original may have been read by an English-only voice
    (Inworld's Alex, Gemini's Achernar), and handing such a voice a language label produces the
    English phonetics of a foreign script rather than the language.

    The language list is filtered to what Seed can pronounce, so there is no fallback branch
    here to get wrong - every entry has a native speaker by construction.
    """
    if not language.get("seed_voice"):
        raise MultiLanguageError(
            f"{language['name']}: no Seed Speech voice - it should not be in LANGUAGES.")
    return (language["seed_voice"], language["seed_language"], pipeline.SEED_SPEECH_TTS_MODEL)


def subtitles_for_track(audio_path, script, out_path=None, status_cb=None):
    """Subtitles for one translated track, timed against THAT track's own audio.

    The English cue times do not transfer: a sentence takes a different number of seconds in
    German than in Japanese, so reusing the original timings would drift further with every
    line. Each language is force-aligned against the audio actually generated for it.

    Returns None rather than raising - subtitles are an addition to a track that already works.
    """
    import subtitles as srt_rules
    audio_path = Path(audio_path)
    out_path = Path(out_path) if out_path else audio_path.with_suffix(".srt")
    try:
        import voice_align
        if not voice_align.available():
            if status_cb:
                status_cb(f"  {out_path.name}: faster-whisper unavailable, no subtitles.")
            return None
        heard = voice_align.transcribe_words(str(audio_path), status_cb=None)
        if not heard:
            return None
        # Align the KNOWN translation onto what was heard: the transcript of a synthetic voice
        # in a second language is the least reliable text available, and we already have the
        # exact words that were sent to the engine.
        aligned = voice_align.align_script_to_words(str(script or ""), heard) or heard
        cues = srt_rules.subtitle_cues(aligned)
        if not cues:
            return None
        srt_rules.write_srt(cues, out_path)
        if status_cb:
            status_cb(f"  {out_path.name}: {len(cues)} cue(s).")
        return out_path
    except Exception as exc:                                            # noqa: BLE001
        if status_cb:
            status_cb(f"  {out_path.name}: no subtitles ({type(exc).__name__}: {exc}).")
        return None


def track_path(out_dir, stem, language):
    """Where a language's audio lives. The code in the name is what YouTube's uploader reads."""
    return Path(out_dir) / f"{stem}.{language['code']}.mp3"


def speak_long(text, out_path, voice, engine, language_string, options=None, status_cb=None,
               cancel_event=None, speak=None):
    """Speak a script of ANY length into one file.

    A twenty-minute narration is far past every engine's per-request character limit, so it is
    split on sentence boundaries the same way the original voiceover is and the parts are
    concatenated. Speaking it in one call was the first version of this and failed on every
    language for exactly the scripts the feature exists for.
    """
    import longform_video                                    # imported late: it imports us back
    speak = speak or pipeline.generate_speech_gemini
    options = dict(options or {})
    out_path = Path(out_path)
    limit = longform_video.tts_chunk_limit(engine)
    parts = longform_video.split_script_for_tts(text, limit=limit) or [text]
    written = []
    for index, part in enumerate(parts):
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        target = out_path.with_name(f"{out_path.stem}.part{index:02d}.mp3")
        if status_cb and len(parts) > 1:
            status_cb(f"  part {index + 1}/{len(parts)} ({len(part)} chars)...")
        # The project's own Seed settings travel with the narrator: a track read at a different
        # speed or loudness than the original is not the same video in another language.
        speak(part, target, voice=voice, model=engine, language=language_string,
              status_cb=None, cancel_event=cancel_event,
              voice_instruction=options.get("voice_instruction") or None,
              tts_speed=float(options.get("speed", 1.0) or 1.0),
              volume=float(options.get("volume", 1.0) or 1.0),
              pitch=int(options.get("pitch", 0) or 0),
              sample_rate=int(options.get("sample_rate", 24000) or 24000),
              output_format="mp3")
        if not target.is_file() or target.stat().st_size < 2000:
            raise MultiLanguageError(f"part {index + 1} of {len(parts)} produced no audio.")
        written.append(target)
    if len(written) == 1:
        written[0].replace(out_path)
    else:
        longform_video.concat_audio_parts(written, out_path, str(pipeline.find_ffmpeg()))
        for part in written:
            part.unlink(missing_ok=True)
    return out_path


def build_tracks(script, out_dir, stem="voiceover", tts_model="pro", model=None,
                 languages=None, status_cb=None, cancel_event=None, tts_options=None,
                 speak=None, translate=None):
    """Translate and speak `script` in every language. Returns [{code, name, audio, script}].

    One language failing never stops the others, and never stops the video: the finished render
    is already the deliverable, and these are extra tracks on top of it.

    `speak` and `translate` exist so the sequence can be tested without buying anything.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    languages = list(languages if languages is not None else languages_for())
    speak = speak or pipeline.generate_speech_gemini
    translate = translate or translate_script
    options = dict(tts_options or {})
    done, failed = [], []
    for language in languages:
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        try:
            translated = translate(script, language, model=model, status_cb=status_cb)
            voice, language_string, engine = voice_for(language, tts_model)
            audio = track_path(out_dir, stem, language)
            (audio.with_suffix(".txt")).write_text(translated, encoding="utf-8")
            if status_cb:
                status_cb(f"{language['name']}: speaking with {voice} ({engine})...")
            speak_long(translated, audio, voice, engine, language_string, options=options,
                       status_cb=status_cb, cancel_event=cancel_event, speak=speak)
            if not audio.is_file() or audio.stat().st_size < 4000:
                raise MultiLanguageError(f"{language['name']}: the engine returned no audio.")
            # Subtitles per language, timed against that language's own audio - the same
            # sentence is not the same number of seconds in German and in Japanese.
            srt = subtitles_for_track(audio, translated, status_cb=status_cb)
            done.append({"code": language["code"], "name": language["name"],
                         "audio": str(audio), "script": str(audio.with_suffix(".txt")),
                         "srt": str(srt) if srt else "",
                         "voice": voice, "engine": engine})
            if status_cb:
                status_cb(f"{language['name']}: track ready ({audio.stat().st_size // 1024} KB).")
        except Exception as exc:                                        # noqa: BLE001
            failed.append(language["name"])
            if status_cb:
                status_cb(f"{language['name']}: skipped ({type(exc).__name__}: {exc}).")
    if status_cb:
        summary = f"Extra audio tracks: {len(done)}/{len(languages)} produced."
        if failed:
            summary += " Missing: " + ", ".join(failed) + "."
        status_cb(summary)
    return done


def write_manifest(tracks, out_path):
    """A machine-readable list of the tracks, next to them.

    A folder of ``name.es.mp3`` files says which language each is but not which voice read it or
    whether one is missing, and that is exactly what you need when an upload sounds wrong.
    """
    out_path = Path(out_path)
    out_path.write_text(json.dumps({"tracks": tracks}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return out_path
