// AstrBot API adapter for Mindcraft (incremental mode)
// Uses fixed session_id, sends only latest message + live status each time
// Supports vision: uploads image to /api/v1/file and sends via message segment array

import { getKey, hasKey } from '../utils/keys.js';

function fixEncoding(str) {
    try {
        const buf = Buffer.from(str, 'latin-1');
        return buf.toString('utf-8');
    } catch {
        return str;
    }
}

function extractLiveStatus(systemMessage) {
    // Extract dynamic status from systemMessage.
    // Keep: goal + STATS block (stats, entities, nearby blocks) + INVENTORY
    // Skip: memory, static rules, examples, command docs
    //
    // systemMessage injected structure:
    //   ...static rules...
    //   YOUR CURRENT ASSIGNED GOAL: "..."   (if active)
    //   Summarized memory:'...'             (skip)
    //   STATS
    //   - Position: x: ..., y: ..., z: ...
    //   - Gamemode: survival
    //   - Health: 20 / 20
    //   - Hunger: 20 / 20
    //   - Biome: plains
    //   - Weather: Clear
    //   - Time: Night
    //   - Current Action: Idle
    //   - Nearby Human Players: ...
    //   - Nearby Bot Players: ...
    //   [modes docs]
    //   ENTITIES
    //   - zombie (12 blocks)
    //   NEARBY_BLOCKS
    //   - stone, dirt
    //   INVENTORY
    //   - diamond_pickaxe: 1
    //   WEARING: Nothing
    //   $COMMAND_DOCS (skip)
    //   $EXAMPLES (skip)
    //   Conversation Begin: (skip)

    const lines = systemMessage.split('\n');
    const result = [];
    let section = 'scan';

    for (const line of lines) {
        const t = line.trim();
        if (!t) continue;

        // Capture current goal
        if (t.startsWith('YOUR CURRENT ASSIGNED GOAL')) {
            result.push('[Goal] ' + t);
            continue;
        }

        // Skip memory section
        if (t.startsWith('Summarized memory')) {
            section = 'memory';
            continue;
        }
        if (section === 'memory') {
            if (t === 'STATS' || t.startsWith('---') || t.startsWith('!') || t.startsWith('$')) {
                section = 'scan';
            } else {
                continue;
            }
        }

        // Capture STATS block (+ ENTITIES + NEARBY_BLOCKS)
        if (t === 'STATS' || t === 'ENTITIES' || t === 'NEARBY_BLOCKS') {
            section = 'stats';
            continue;
        }
        if (section === 'stats') {
            if (t === 'INVENTORY') {
                section = 'inventory';
                continue;
            }
            if (t.startsWith('$EXAMPLES') || t.startsWith('Conversation Begin') || t.startsWith('!')) {
                section = 'scan';
                continue;
            }
            result.push(t);
            continue;
        }

        // Capture INVENTORY block
        if (t === 'INVENTORY') {
            section = 'inventory';
            continue;
        }
        if (section === 'inventory') {
            if (t.startsWith('$EXAMPLES') || t.startsWith('Conversation Begin') || t.startsWith('!')) {
                section = 'scan';
                continue;
            }
            result.push(t);
            continue;
        }
    }

    // Fallback: take last meaningful lines
    if (result.length === 0) {
        const relevant = [];
        const skipPrefixes = ['Conversation', '$', 'You are', 'Be a', 'Do NOT',
                             'Respond only', 'If you have', 'This is', 'Summarized memory'];
        for (let i = lines.length - 1; i >= 0; i--) {
            const t = lines[i].trim();
            if (!t) continue;
            if (skipPrefixes.some(p => t.startsWith(p))) continue;
            relevant.unshift(t);
            if (relevant.length >= 20) break;
        }
        return relevant.join('\n');
    }

    return result.join('\n');
}

export class AstrBot {
    static prefix = 'astrbot';

    constructor(model_name, url, params) {
        this.model_name = model_name;
        this.params = params || {};
        this.baseUrl = (url || getKey('ASTRBOT_API_URL') || 'http://localhost:6185').replace(/\/+$/, '');
        this.apiKey = getKey('ASTRBOT_API_KEY');
        this.configName = (params && (params.config_name || params.configName)) ||
                          getKey('ASTRBOT_CONFIG_NAME') || 'minecraft';
        this.username = (params && (params.username || params.userName)) || 'MindcraftBot';
        this.botName = model_name || 'astrbot';
        this.fetchTimeout = (params && params.timeout) || 60000;
    }

    // Upload file to AstrBot and get attachment_id
    async uploadFile(imageBuffer, filename = 'screenshot.png') {
        const url = this.baseUrl + '/api/v1/file';
        const boundary = '----FormBoundary' + Math.random().toString(36).slice(2);

        let headerStr = '';
        headerStr += '--' + boundary + '\r\n';
        headerStr += 'Content-Disposition: form-data; name="file"; filename="' + filename + '"\r\n';
        headerStr += 'Content-Type: image/png\r\n\r\n';

        const encoder = new TextEncoder();
        const headerBytes = encoder.encode(headerStr);
        const footerBytes = encoder.encode('\r\n--' + boundary + '--\r\n');

        const totalLength = headerBytes.length + imageBuffer.length + footerBytes.length;
        const combined = new Uint8Array(totalLength);
        combined.set(headerBytes, 0);
        combined.set(new Uint8Array(imageBuffer), headerBytes.length);
        combined.set(footerBytes, headerBytes.length + imageBuffer.length);

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), this.fetchTimeout);

        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + this.apiKey,
                'Content-Type': 'multipart/form-data; boundary=' + boundary
            },
            body: combined,
            signal: controller.signal
        });

        clearTimeout(timeoutId);

        if (!response.ok) {
            const errText = await response.text();
            throw new Error('Upload failed: ' + response.status + ' ' + errText);
        }

        const result = await response.json();
        return result.data?.attachment_id || result.attachment_id;
    }

    // Send chat request (text only, incremental)
    async sendRequest(turns, systemMessage, stop_seq = '***') {
        const latestTurn = turns.length > 0 ? turns[turns.length - 1] : null;
        const latestContent = latestTurn ? (latestTurn.content || '') : '';
        const latestRole = latestTurn ? latestTurn.role : 'user';

        let cleanContent = latestContent;
        while (cleanContent.includes(stop_seq)) {
            cleanContent = cleanContent.replace(stop_seq, '');
        }

        const liveStatus = extractLiveStatus(systemMessage);

        let messageText = '';
        if (liveStatus) {
            messageText += '[Current Status]\n' + liveStatus + '\n\n';
        }
        messageText += '[' + (latestRole === 'assistant' ? 'Assistant' : 'User') + '] ' + cleanContent;

        const sessionId = 'mindcraft_' + this.botName;
        const payload = {
            username: this.username,
            session_id: sessionId,
            message: messageText,
            config_name: this.configName,
            enable_streaming: false
        };

        return this._callChatApi(payload, stop_seq);
    }

    // Send vision request (uploads image, sends as message array)
    async sendVisionRequest(messages, systemMessage, imageBuffer) {
        const sessionId = 'mindcraft_' + this.botName;

        try {
            console.log('[AstrBot] Uploading screenshot for vision...');
            const attachmentId = await this.uploadFile(imageBuffer);
            console.log('[AstrBot] Image uploaded, attachment_id:', attachmentId);

            const messageSegments = [
                { type: 'plain', text: systemMessage },
                { type: 'image', attachment_id: attachmentId }
            ];

            const payload = {
                username: this.username,
                session_id: sessionId,
                message: messageSegments,
                config_name: this.configName,
                enable_streaming: false
            };

            console.log('[AstrBot] Sending vision request for [' + this.botName + ']...');
            return await this._callChatApi(payload);

        } catch (err) {
            console.error('[AstrBot] Vision upload failed, fallback to text:', err.message || err);
            const fallbackMsg = systemMessage + '\n[Image data: ' + imageBuffer.length + ' bytes]';
            const payload = {
                username: this.username,
                session_id: sessionId,
                message: fallbackMsg,
                config_name: this.configName,
                enable_streaming: false
            };
            return await this._callChatApi(payload);
        }
    }

    // Core chat API call
    async _callChatApi(payload, stop_seq) {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), this.fetchTimeout);

            const response = await fetch(this.baseUrl + '/api/v1/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + this.apiKey
                },
                body: JSON.stringify(payload),
                signal: controller.signal
            });

            clearTimeout(timeoutId);

            if (!response.ok) {
                const errText = await response.text();
                console.error('[AstrBot] API error:', response.status, errText);
                return 'My brain disconnected, try again.';
            }

            const text = await response.text();
            const lines = text.split('\n');
            let result = '';

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const dataStr = line.slice(6).trim();
                if (!dataStr || dataStr === ': heartbeat') continue;

                try {
                    const data = JSON.parse(dataStr);
                    if (data.type === 'plain' && data.chain_type !== 'tool_call') {
                        const content = fixEncoding(data.data || '');
                        if (content) result += content;
                    } else if (data.type === 'end') {
                        break;
                    }
                } catch {
                    continue;
                }
            }

            if (stop_seq) {
                const stopIndex = result.indexOf(stop_seq);
                if (stopIndex !== -1) result = result.slice(0, stopIndex);
            }

            return result || '...';

        } catch (err) {
            if (err.name === 'AbortError') {
                console.error('[AstrBot] Request timed out after', this.fetchTimeout, 'ms');
            } else {
                console.error('[AstrBot] Error:', err.message || err);
            }
            return 'My brain disconnected, try again.';
        }
    }

    async embed(text) {
        console.log('[AstrBot] embed() - returning mock zero vector of length 128');
        return new Array(128).fill(0);
    }
}
