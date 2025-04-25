import express from 'express';
import bodyParser from 'body-parser';
import { createTTS } from "mi-gpt-tts";
import dotenv from 'dotenv';
import path from 'path';
import { fileURLToPath } from 'url';
import { writeFile } from "fs/promises";
dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);1

class TTSServer {
    constructor() {
        this.app = express();

        this.tts = createTTS({
            defaultSpeaker: process.env.TTS_DEFAULT_SPEAKER || "BV001_streaming",
            volcano: {
                appId: process.env.VOLCANO_TTS_APP_ID,
                accessToken: process.env.VOLCANO_TTS_ACCESS_TOKEN,
                userId: process.env.VOLCANO_TTS_USER_ID,
            },
            edge: {
                trustedToken: process.env.EDGE_TTS_TRUSTED_TOKEN,
            },
            openai: {
                apiKey: process.env.OPENAI_API_KEY,
                model: process.env.OPENAI_TTS_MODEL,
                baseUrl: process.env.OPENAI_BASE_URL,
            },
        });

        this.setupMiddleware();
        this.setupRoutes();
    }

    setupMiddleware() {
        // 1. First try to get raw body
        this.app.use(express.raw({
            type: '*/*',
            limit: '10mb',
            verify: (req, res, buf) => {
                req.rawBody = buf;
            }
        }));
    
        // 2. Parse raw body if needed
        this.app.use((req, res, next) => {
            if (Buffer.isBuffer(req.body)) {
                try {
                    const bodyStr = req.body.toString('utf8');
                    console.log('Raw body received:', bodyStr);
                    req.body = JSON.parse(bodyStr);
                    console.log('Successfully parsed raw body to JSON');
                } catch (e) {
                    console.error('Failed to parse raw body:', e);
                }
            }
            next();
        });
    
        // 3. Standard JSON parser
        this.app.use(express.json({
            type: ['application/json', 'text/plain'],
            strict: false,
            verify: (req, res, buf) => {
                if (typeof req.body === 'string') {
                    try {
                        req.body = JSON.parse(buf.toString());
                    } catch (e) {
                        console.error('Failed to parse JSON string body:', e);
                    }
                }
            }
        }));
    
        // 4. URL encoded parser
        this.app.use(express.urlencoded({ 
            extended: true,
            limit: '10mb'
        }));
    
        // 5. Content negotiation middleware
        this.app.use((req, res, next) => {
            const contentType = req.get('content-type');
            console.log('Content-Type:', contentType);
            
            if (!req.body || Object.keys(req.body).length === 0) {
                try {
                    const rawBody = req.rawBody ? req.rawBody.toString() : null;
                    if (rawBody) {
                        console.log('Attempting to parse empty body from raw data');
                        req.body = JSON.parse(rawBody);
                    }
                } catch (e) {
                    console.error('Failed to parse body from raw data:', e);
                }
            }
            next();
        });
    
        // 6. Debug logging middleware
        this.app.use((req, res, next) => {
            console.log('==== Request Debug Info ====');
            console.log('Request Headers:', req.headers);
            console.log('Request Body Type:', typeof req.body);
            console.log('Request Body:', JSON.stringify(req.body, null, 2));
            console.log('========================');
            next();
        });
    
        // 7. IP access control middleware
        this.app.use((req, res, next) => {
            const ip = req.ip || req.connection.remoteAddress;
            const allowedIPs = ['127.0.0.1', '::1', 'localhost', '::ffff:127.0.0.1'];
    
            if (!allowedIPs.includes(ip) && !ip.includes('127.0.0.1')) {
                return res.status(403).json({
                    reqid: `tts-${Date.now()}`,
                    code: 4003,
                    operation: "query",
                    message: "Access denied",
                    sequence: -1,
                    data: ""
                });
            }
            next();
        });
    
        // 8. Error handling middleware
        this.app.use((err, req, res, next) => {
            console.error('Middleware Error:', err);
            res.status(500).json({
                reqid: `tts-${Date.now()}`,
                code: 5000,
                operation: "query",
                message: `Server Error: ${err.message}`,
                sequence: -1,
                data: ""
            });
        });
    }

    setupRoutes() {
        this.app.post('/api/v1/tts', async (req, res) => {
            try {
                console.log('Processing TTS request');
                // Extract parameters from request body
                const requestBody = req.body;

                console.log(`Received TTS request: ${JSON.stringify(requestBody)}`);
                const reqid = requestBody.request?.reqid || `tts-${Date.now()}-${Math.random().toString(36).substring(2, 8)}`;
                const text = requestBody.request?.text;
                const operation = requestBody.request?.operation || "query";
                const voice_type = requestBody.audio?.voice_type || process.env.TTS_DEFAULT_SPEAKER || "BV001_streaming";

                // Validate required parameters
                if (!text) {
                    return res.json({
                        reqid,
                        code: 4000,
                        operation,
                        message: "Missing required parameter: text",
                        sequence: -1,
                        data: ""
                    });
                }

                console.log(`Processing TTS request: ${reqid}, text: "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}"`);

                // Call TTS processing
                const audioBuffer = await this.tts({
                    text,
                    speaker: voice_type,
                });

                // Process audio data
                if (audioBuffer) {
                    await writeFile("output.wav", audioBuffer);

                    const base64Audio = audioBuffer.toString('base64');

                    // Return simplified response format
                    res.json({
                        reqid,
                        code: 3000,
                        operation,
                        message: "Success",
                        sequence: -1,
                        data: base64Audio
                    });

                    console.log(`TTS request ${reqid} completed, generated ${audioBuffer.length} bytes of audio`);
                } else {
                    res.json({
                        reqid,
                        code: 5000,
                        operation,
                        message: "Failed to generate audio",
                        sequence: -1,
                        data: ""
                    });
                }
            } catch (error) {
                console.error("TTS API error:", error);
                res.json({
                    reqid: req.body.request?.reqid || `tts-${Date.now()}`,
                    code: 5000,
                    operation: req.body.request?.operation || "query",
                    message: `Error: ${error.message || "Unknown error"}`,
                    sequence: -1,
                    data: ""
                });
            }
        });

       

        this.app.get('/api/v1/health', (req, res) => {
            res.json({
                code: 3000,
                message: "Service is healthy",
                data: ""
            });
        });
    }

    start(port = 50000) {
        this.app.listen(port, '127.0.0.1', () => {
            console.log(`TTS Server running at http://127.0.0.1:${port}`);
        });
    }
}

// Start the server
const server = new TTSServer();
server.start();