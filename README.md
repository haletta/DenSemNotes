# DenSemNotes for Mac

Take notes in class while DenSemNotes listens. It transcribes the lecture on your Mac, summarizes it as it goes, shows your slides beside your notes, answers questions from everything you've written, and turns a note into your MyNotes lesson.

## Install from GitHub

1. On the repository page, click **Code › Download ZIP** and unzip it (or `git clone` it).
2. Follow the steps below from the unzipped folder.

After that the app keeps itself up to date (see **Updates**).

## Install (or update)

1. Unzip the download.
2. Open **Terminal** (⌘Space, type "Terminal").
3. Type `bash ` (with a space), drag **Install.command** from the unzipped folder into the Terminal window, and press Return.
4. Wait about 5 minutes the first time (it downloads Python, the speech libraries and the speech model, about 1 GB).

**DenSemNotes** appears in the Applications folder inside your home folder. Updating keeps all your courses, classes and settings.

The first time you open it, macOS may say it's from an unidentified developer: right-click the app, choose **Open**, then **Open** again. Click **Allow** when it asks for the microphone.

## The window

| Area | What's there |
|---|---|
| **Title bar** | Window buttons, new class, the class name and word count, **Record**, **What did I miss?**, the four inspector buttons (Live · Ask · Transcript · Slides), **Aa**, **Focus**, **Lesson & export**, **Preferences** |
| **Sidebar** | **Search** at the top (searches every course), **Library** (All classes, Recent), **Courses** with their classes nested inside, and **Tags** |
| **Document list** | Cards for the classes in what you picked: name, day, a preview of your notes, length, slides, lesson badge and tags |
| **Editor** | Your note in a centred column, formatted as you type, with the formatting bar above it |
| **Inspector** | Live summaries, Ask, Transcript and Slides, opened from the title bar |

- **Courses**: the + in the sidebar adds one. Right-click a course to rename it, change its icon and colour, or start a class in it.
- **Classes** are named by date (`2026-10-26-Notes`) and sit inside their course in the sidebar. Click the arrow next to a course to show or hide its classes, and click a class to open it; a green dot marks a class that's been made into a lesson. Double-click a class to rename it, or right-click it for more.
- **Moving a class**: drag it (from the sidebar or the card list) onto another course.
- **Search** (⌥⌘F): type in the box at the top of the sidebar to search every course at once. Matching classes show under their courses and as cards; Return opens the first one, Esc clears the search.
- **Tags**: type `#exam` or `#enzymes` anywhere in a note. Tags collect in the sidebar. Lessons don't add tags of their own.

## Writing

The editor shows formatting as you type; the Markdown symbols appear only on the line you're editing. The formatting bar holds the tools you choose in **Preferences › Editor**: headings, bold, italic, key term, code, lists, checklist, quote, callouts, Q/A, verse, divider, link to slide, spelling, show Markdown, and **Format as lesson**.

- Each line remembers **when** you wrote it and **which slide** was on screen; both show in the left margin.
- **Spelling is corrected as you type** (clear typos only, never names or terms you've taught it). ⌘Z undoes a correction. Words you **Learn** in the spelling check are added to the course's dictionary.

## Appearance

- **Aa** in the title bar changes the font (New York serif, SF Pro, SF Mono), size, line height, line width, theme (Auto, Light, Dark) and accent colour, live.
- **Preferences** (⌘,) has four tabs: **General** (microphone, speech model, language, live summary interval), **Appearance** (theme, accent, translucent sidebar), **Editor** (type, spelling, margin, formatting tools, lesson mix), **Shortcuts** and **AI**.

## Zoom

Make the note bigger or smaller without changing your font settings: **⌘+** and **⌘−**, **⌘0** for 100%, pinch on the trackpad over the note, or the **− 100% +** control at the bottom right of the note. Read view and writing keep their own zoom, so you can read lessons large and write at your usual size.

## Discreet recording

Turn it on in **Preferences › General › Recording**, or right-click the record button. The red **Record** button with its timer becomes a small grey circle: hollow when it isn't recording, filled in when it is, with no red, words, timer or animation. Point at it to see the time; click it to start or pause as usual. The Recording tag on the class card and the red dot in Live are hidden too.

macOS still shows its own small microphone dot in the menu bar whenever any app uses the microphone; apps can't hide it. Check your school's policy on recording lectures.

## Focus mode (⌘D)

Hides the sidebar, document list and inspector, and fades the title bar and formatting bar until you point at them. Esc or ⌘D leaves.

## Import a past class (⇧⌘I)

For a class DenSemNotes didn't record. Click the import button (the arrow into a tray) at the top of the class list, right-click a course › **Import a past class…**, or press ⇧⌘I. Then:

1. Pick the **course** and the **date** of the class (the class is named by that date, like `2026-10-15-Notes`).
2. Add the **lecture**: a transcript file, pasted text, or a recording.
   - Transcripts: text, Word, PDF, RTF, subtitles (.srt, .vtt), and Zoom, Teams or Otter exports. Their timestamps are used; a transcript without times gets times worked out from a normal speaking pace.
   - Recordings: audio or video (.mp3, .m4a, .wav, .mp4, .mov…), transcribed on your Mac with the same offline speech model. A one-hour recording takes a while, and the progress shows as it goes.
3. Add **slides and handouts** (optional). Each part of the lecture is linked to the slide it's about.
4. Paste **notes you took** in that class (optional). They're kept and blended into the class notes.
5. Press **Import**. The app goes straight to your **class notes** in your MyNotes template: it reads the whole lecture and slides at once, without making 10-minute part summaries or note-helper picks along the way. (Tick **Also 10-minute part summaries and note helper items** if you want those too.)

A checklist shows each step. You can close the window; the class fills in as it goes and a message says when it's done. Imported classes show an **Imported** tag on their card and work like any other class: Ask, Read view, Format as lesson, export.

## Format as lesson (⇧⌘F)

Turns the note itself into your MyNotes template: front matter, one title, sections, callouts, ==key terms== with a glossary, Q:/A: questions and a diagram. It blends your writing with the lecture summaries, transcript and slides; choose the mix from the arrow next to the button (Mostly my notes, Balanced, Mostly the lecture, Only my notes).

**New lesson from transcript & slides** (in the Format as lesson menu, the Lesson & export menu, or right-click any class in the list) writes a fresh lesson from what a class already has: its transcript, slides and documents, plus the notes you wrote yourself (as they were before any earlier formatting, never from an earlier lesson). Use it to remake a lesson in the new style or after adding slides. The current lesson is kept: **Restore** brings it back.

There's no extra window: the formatted lesson replaces the note in the editor. A bar above it shows the upload checks, **Save .md** and **Restore original**. Your original note is always kept.

### Built from your template

The lesson follows the **AI Conversion Prompt** in your class note template. The built-in template asks for a readable, lively study page you can review in 5–10 minutes:

- front matter, one title and an IMPORTANT callout with the main idea;
- sections in the order taught, each opening with why it matters, then short bullets with the key phrase in **bold**;
- the lecturer's own words, examples and stories (EXAMPLE callouts), quotes with who said them, and books with their *titles*, plus short clarifying details where they help;
- ==key terms== with a Glossary, and a short Summary at the end;
- steps, tabs, a diagram, a verse block or questions **only when the lecture calls for them**;
- no greetings, logistics, repeats or filler, no tags, and no Review questions.

**Edit the template** in **Preferences › Editor › Class note template › Edit…**. **Go to AI Conversion Prompt** jumps to the part the AI follows; change it, press **Save**, and your next class notes follow it. **Use the built-in one** goes back.

### Read view

A finished lesson opens in **Read** view, looking the way MyNotes shows it: sans-serif headings, serif body, bullets with **bold** key phrases, callout cards with an icon, spaced label and a chevron to fold them, and key terms underlined with dots (click one for its definition).

- Self-check answers reveal on click, tabs switch, steps go Back/Next, diagrams draw (offline), and verse cards open the passage on Bible Gateway.
- **Read / Edit** at the top of the lesson switches views (⌥⌘R). The app remembers your choice for the next lesson.

## Live summaries, Ask, Transcript, Slides

- **Live** has two halves; drag the line between them to resize, or double-click it to reset.
  - **Summary** (top): every 10 minutes (change it in Preferences › General) the part just heard gets a summary card with its details; pausing puts them together into a **Whole lecture** summary. **What did I miss?** (⌘K) recaps the last few minutes.
  - **Note helper** (bottom): while the lecturer talks it picks out **key points**, **quotes** (with who said them), ideas from a **book or author** (with the title), **key terms** with their meaning, **examples** and stories, **questions** and **due dates**, newest at the bottom. They're spotted the instant they're said; then the AI tidies them and adds what the quick check missed (every 2 minutes by default; change it or turn the AI part off in Preferences › General › Note helper). Filter by kind with the chips. **+** puts an item in your notes already formatted: a quote as a `>` quote with the name, a book as **Author**, *Title*, a term as ==term==, an example as an EXAMPLE callout. Lessons use them too.
- **Ask**: ask anything about this class, this course or all courses. Answers cite numbered sources you can click.
- **Slides**: upload or drop PowerPoint, Keynote, Word, Pages, PDF, text or photos. Slides show with their pictures, illustrations, shapes, tables and charts. Notes typed while a slide is showing are linked to it. **Follow** jumps to the slide the lecturer seems to be on.
- **Studying with slides**: summaries and class notes are written with the slides' text, and each section of a lesson is linked to the slide it covers. In Read view a small slide tag (S4) sits beside each part; click it to open that slide. With **Follow my notes** on (in the Slides tab), scrolling your notes turns the slides to match the part at the top of the screen; clicking a tag yourself wins until you scroll again. Lessons made before this, or that get slides later, are linked automatically the first time you open them.

## Shortcuts

Change any of them in **Preferences › Shortcuts**: click a shortcut and press the new keys. Delete clears it, Esc cancels, **Reset** puts one back and **Restore all defaults** resets them all. If the keys you press are already used, they move to the new action and the old one is cleared. Keys macOS needs (⌘C, ⌘V, ⌘Z, ⌘Q…) can't be taken.

| General | | Lecture | |
|---|---|---|---|
| New class | ⌘N | Record / pause | ⌘. |
| New course | ⌥⌘N | What did I miss? | ⌘K |
| Import a past class | ⇧⌘I | | |
| Search all classes | ⌥⌘F | Live · Ask · Transcript · Slides | ⌘1 · ⌘2 · ⌘3 · ⌘4 |
| Next / previous class | ⌥⌘↓ / ⌥⌘↑ | Next / previous slide | ⌥⌘→ / ⌥⌘← |
| Preferences | ⌘, | Link line to slide | ⇧⌘L |

| View | | Lesson | |
|---|---|---|---|
| Library | ⌘\ | Format as lesson | ⇧⌘F |
| Focus mode | ⌘D | Save lesson as .md | ⌘S |
| Read / Edit lesson | ⌥⌘R | Check spelling | ⌘; |
| Appearance (Aa) | ⌘T | | |
| Light / dark | ⌥⌘T | | |
| Show Markdown | ⇧⌘M | | |
| Zoom in / out / 100% | ⌘+ / ⌘− / ⌘0 | | |

**While typing a note:** bold ⌘B, italic ⌘I, key term ⌘E, title / section / sub-point ⌥⌘1 / ⌥⌘2 / ⌥⌘3, bullets ⇧⌘8, numbered ⇧⌘7, checklist ⇧⌘9, Q/A ⇧⌘Q, Note / Warning / Important callout ⌥⌘C / ⌥⌘W / ⌥⌘I, verse block ⌥⌘V. Inline code, quote and divider have no shortcut until you give them one.

## AI (Preferences › AI)

- **AI** (top): writes the live summaries, the note helper and Ask answers.
  - **Ollama** (default, free, offline): install from ollama.com, then click **Download** next to `llama3.2`.
  - **Google Gemini** or **Groq** (free tiers, need internet): paste a free key.
  - **Claude** (API key), or **None** (key sentences only).
- **Class notes**: who writes your class notes (Format as lesson and imported classes). It can differ from the AI above:
  - **Claude Code: my Claude plan, no API key.** Uses the Claude subscription you already have (Pro, Max…); its use counts toward your plan's normal limits. One-time setup: open Terminal, paste `curl -fsSL https://claude.ai/install.sh | bash`, then open a new Terminal window, type `claude`, press Return and sign in with your Claude account. Back in Preferences press **Check again**, then **Test**. Choose **Sonnet** (recommended), **Opus** (most careful) or **Haiku** (fastest).
  - **Claude with my API key**, or **The same AI as above**.

The class-notes AI reads the lecture, your notes and the text of every slide deck and PDF in the class, so you can also make class notes from slides and handouts alone (import them with no script). If the AI is unavailable, the app keeps going with basic summaries and a basic lesson.

## Updates

The app checks the repository's **main** branch when it opens and every few hours. When there's a new commit, an **Update** button appears in the title bar.

To check yourself: **Help › Check for Updates…** in the menu bar, right-click the **Preferences** (gear) button › **Check for updates…**, or give it a shortcut in Preferences › Shortcuts. It tells you straight away when you're up to date, or opens **Update now** when there's something new.

**Preferences › General › Updates** lists what changed (the commit messages) and has:

- **Update now**: downloads that version from GitHub, keeps your current one, installs any new libraries, then asks you to **Restart now**. Your classes, notes and settings are never touched.
- **Go back to the previous version**: if an update misbehaves.
- **Check now**, and a switch to turn automatic checks off.
- **Private repository?**: paste a GitHub token with read-only access to the repository's contents. A public repository needs no token.

Updates come from the repository named in `update.json` (haletta/DenSemNotes). Updating is paused while you're recording.

## Troubleshooting

- **Quitting**: closing the window or ⌘Q quits at once, even while recording. Your notes and everything heard so far are saved; the lecture's final summary is made in the background the next time you open the app. Programs DenSemNotes started (Claude Code, slide conversions, and Keynote or Pages if it opened them) are closed too, and a local Ollama model is unloaded. An update that's already installing is left to finish by itself.

- **Mic bars stay grey**: System Settings › Privacy & Security › Microphone › turn on DenSemNotes, then reopen the app.
- **Slides look different from PowerPoint**: Keynote gives an exact copy. If macOS hasn't allowed DenSemNotes to use Keynote, the app draws PowerPoint slides itself (pictures, shapes, tables, charts and SmartArt are kept, but fancy effects and some fonts may differ). To get exact slides: System Settings › Privacy & Security › Automation › DenSemNotes › turn on Keynote, then in the Slides tab choose **⋯ › Convert again**.
- **Slides won't convert at all**: export them as PDF and upload the PDF.
- **Log file**: `~/Library/Application Support/DenSemNotes/app.log`
- **Uninstall**: delete `~/Applications/DenSemNotes.app` and `~/Library/Application Support/DenSemNotes`. Saved files in `Documents/DenSemNotes` stay.

Check your school's policy on recording lectures before using it.
