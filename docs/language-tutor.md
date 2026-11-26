# Language Tutor

Personal Hungarian/English conversation practice in the web UI (**Nyelvi tutor**, part of the "Tanulás és beszéd" area). Everything is stored per person, in their personal workspace; nobody else sees another person's profile, sessions, progress or saved words.

## What it does

- **Learner profile**: target language (`en` or `hu`), CEFR-like level (A1–C2) and a free-text goal. The level can be set by hand, or adjusted automatically: after every five graded answers it goes up one step if all five were error-free and down one step if none were. Each change is recorded with its reason (`manual`/`auto`).
- **Practice modes**: conversation, role-play (the tutor plays a role in the chosen scenario), listening comprehension (a short text is read aloud with the transcript hidden, then a question is asked) and written correction.
- **Corrections**: every answer can come back with corrections typed `grammar`, `spelling`, `vocabulary` or `comprehension` (`other` when the model names something else), each with a Hungarian explanation and, where it fits, a correct example sentence in the target language.
- **Own material**: a session can be based on a library document (retrieved through the normal RAG search, limited to workspaces the user can read) and/or the words of one of the user's own flashcard decks.
- **Vocabulary to cards**: useful phrases the tutor suggests can be saved into an existing deck or into an automatic "Nyelvi gyakorlás · EN/HU" deck, where they join the normal spaced-repetition review.
- **Progress**: number of sessions and answers and errors by type for the current target language, and the history of level changes for every language.

## Voice

Voice practice needs the voice overlay with an STT model and a Piper voice for the target language (an English voice, `en_US-lessac-medium`, is in the default voice list). The microphone needs HTTPS or `localhost`.

- **Push-to-talk**: hold the button while speaking; release sends the answer.
- **Hands-free mode**: one tap starts a loop: listen, detect the end of speech by a short silence, transcribe, send, read the tutor's reply aloud, listen again. No screen interaction is needed mid-session: the screen is kept on with the Wake Lock API (where the browser lacks it, the UI asks to turn auto-lock off), a rising tone means it is listening and a falling tone that the session stopped. If nothing is heard, it listens once more. Saying "gyakorlás vége" or "end practice", a long silence, leaving the page or locking the screen ends it.
- **Measured speech data**: with `measure=true` the STT returns only what it can measure: recording length, words per minute over the span from the first to the last word (waits before and after speaking don't count), the number of pauses of at least half a second between words, and the mean word recognition confidence. These are shown as plain numbers. The UI states that they do not replace a human teacher's assessment; the tutor model is told not to judge pronunciation or accent.

## API

All routes need the normal login and CSRF token.

| Method | Path | Purpose |
| --- | --- | --- |
| GET/PUT | `/tutor/profile` | read or change `{language, level, goal, auto_level}` |
| GET | `/tutor/decks` | the user's own flashcard decks |
| POST | `/tutor/sessions` | start `{mode, scenario?, source_path?, deck_id?}`; returns the session with the tutor's opening turn |
| GET | `/tutor/sessions/{id}` | the session and its turns |
| POST | `/tutor/sessions/{id}/turns` | answer `{text, revision?}`; `409` if the session changed meanwhile, `502` if the model's reply is unusable (nothing is stored then) |
| POST | `/tutor/sessions/{id}/cards` | save `{front, back, deck_id?}` into a deck |
| GET | `/tutor/progress` | sessions, answers, errors by type, level changes |

The prompt lives in `config/prompts/tutor_conversation.md`. The model must return one JSON object (`reply`, `corrections`, `vocabulary`). Small slips are tolerated: missing lists become empty, lists are cut to six items, unknown correction types become `other`, and items without text are dropped. Only a missing or empty `reply` (or no JSON) is refused.

## Tables

`tutor_profiles`, `tutor_sessions`, `tutor_turns` and `tutor_level_changes`, created by `create_all`; no numbered migration.

## Tests

`tests/test_tutor_api.py` uses a fake model and retriever: profile and level changes, private material and deck export, per-user isolation, automatic level change, a failed model turn, and listening context. `tests/test_voice_service.py` covers the measured speech data with a fake Whisper model.
