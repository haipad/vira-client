# Vira Desktop Client

Vira is a Windows desktop voice companion. Speak to the avatar to ask questions, save notes, open files and websites, take screenshots, or have a document read or explained. The latest recognized user utterance and Vira's response appear below the avatar.

## What You Can Ask

### Conversation

Vira can answer questions and follow up on the current conversation. The backend can use previous conversations and extracted insights as context.

- "Explain self-attention in simple terms."
- "Can you give me an example?"
- "What did we discuss earlier?"

### Notes And Schedule

Save notes, add dated plans, and ask about them later. Times are interpreted in Indian Standard Time (IST).

- "Remember that my project is called Vira."
- "Note that I have a client meeting tomorrow at 6 PM."
- "What's on my schedule tomorrow?"
- "What did I note about my project?"

Dated notes can be recalled as a schedule. Automatic alerts at the due time are not currently implemented.

### Open Files

Vira searches common user folders for matching filenames and offers up to two ranked choices. Confirm the file to open it in its default application. You can narrow a search by naming a folder or giving more of the filename.

- "Open my resume."
- "Find the budget spreadsheet in Documents."
- "Open the first one."

Search covers Desktop, Documents, Downloads, Pictures, Videos, and Music, including supported document and media formats. Executables and scripts are not opened by this workflow.

### Read Or Explain Documents

Vira can read text aloud, summarize a document, or explain a part of it. It supports PDF, DOCX, TXT, and Markdown files with extractable text.

- "Open my resume and explain it."
- "Read the report I just opened."
- "Explain the experience section of my resume."
- "Summarize pages one through three of the document."

For an open-and-explain request, choose a search result first. Vira then asks for permission before sending document contents to the backend for processing. The most recently opened supported document can be reused for a follow-up while its reference remains valid, currently up to 10 minutes.

Documents are limited to 8 MiB and 24,000 extracted characters. PDFs over 20 pages require a selection of up to five pages; shorter PDFs can be processed in full. Scanned PDFs without extractable text are not supported.

### Websites And Screenshots

- "Open YouTube."
- "Open the browser."
- "Take a screenshot."

Screenshots are saved in the Windows Pictures/Screenshots folder, which may be redirected by Windows or OneDrive. Vira reports the folder after a successful capture.

## Avatar Controls

- Drag the avatar to move it.
- Double-click the avatar to quit.
- Scroll the text below the avatar to read longer utterances and replies. Captions are based on the recognized utterance and generated response, not word-by-word live transcription.

## Setup

### Requirements

- Windows, Python 3.11, a microphone, and speakers or headphones.
- An Agora project with an app ID and certificate, plus Sarvam and Murf API keys.
- A running [Veera backend](../veera-backend/README.md). Agora must be able to reach the backend's public URL for LLM requests.

### 1. Configure And Start The Backend

From the sibling `veera-backend` directory, create a Python environment, install its dependencies, and configure its `.env` with at least:

```dotenv
SARVAM_API_KEY=your_sarvam_api_key
VEERA_DESKTOP_KEY=your_shared_desktop_key
```

Then start the API:

```powershell
cd ..\veera-backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8010
```

Use a secure public HTTPS endpoint or tunnel that Agora can reach and that forwards to the backend. Do not commit credentials or expose secrets in public URLs.

### 2. Configure And Start The Client

In the `vira-client` directory, create a separate environment and install its dependencies:

```powershell
cd ..\vira-client
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create a client `.env` with your own values:

```dotenv
AGORA_APP_ID=your_agora_app_id
AGORA_APP_CERTIFICATE=your_agora_app_certificate
SARVAM_API_KEY=your_sarvam_api_key
MURF_API_KEY=your_murf_api_key
VEERA_DESKTOP_KEY=your_shared_desktop_key
VEERA_BACKEND_URL=http://127.0.0.1:8010
VEERA_PUBLIC_URL=https://your-public-backend-url
VEERA_ACCESS_ID=vira-desktop
VEERA_USER_NAME=User
```

The desktop key must match the backend's key. `VEERA_BACKEND_URL` is used by the local client, while `VEERA_PUBLIC_URL` must be reachable by Agora. Keep both `.env` files private.

Start the client with the same Python environment in which you installed the requirements:

```powershell
.\.venv\Scripts\python.exe main.py
```

The client serves its audio page locally on port 8021 by default. Allow microphone access if prompted. Desktop actions and document processing require the authenticated client to be connected to the backend.

## Troubleshooting

- **No voice:** Check microphone and speaker permissions, the Agora credentials, and whether the public backend URL is reachable from Agora.
- **Desktop actions unavailable:** Check that the backend and client are running and their `VEERA_DESKTOP_KEY` values match.
- **Screenshot fails:** Install the client requirements with the same Python interpreter used to start the client. Pillow is required for capture.
- **Document cannot be read:** Check the format, size, selected PDF pages, and whether the document contains extractable text. Give permission when prompted.
- **Wrong file choices:** Provide more of the filename or specify a folder, then select the correct result.

## Tests

Run the client tests from this directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```