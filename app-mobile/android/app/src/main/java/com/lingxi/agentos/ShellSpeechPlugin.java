package com.lingxi.agentos;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;

import com.getcapacitor.Bridge;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.ArrayList;

/**
 * 壳自建最小插件：Android 系统 SpeechRecognizer 桥（移动端原生语音识别）。
 *
 * 通道契约（消费方=frontend useVoiceInput native 路径）：原生优先，探测不可用
 * （available=false）或启动失败时前端自动回退既有 server-asr 录音转写链路。
 * 事件词表与 Web Speech 错误码同形（speechPartial/speechFinal 携 transcript，
 * speechError 携 code），前端按同形码做回退/报错决策。
 *
 * 权限面：客户端不需要 RECORD_AUDIO（识别服务自持录音），本插件不申请任何权限。
 * 会话语义：start 后 Java 侧静默续听（onResults/onError 暂态错误后延迟重启
 * startListening），stop 后等 onResults/onError 落最终结果；cancel 直接销毁。
 * 线程面：recognizer 全部操作经 bridge.executeOnMainThread 收敛到主线程
 * （SpeechRecognizer 要求主线程），回调本就回主线程，状态字段无并发面。
 */
@CapacitorPlugin(name = "ShellSpeech")
public class ShellSpeechPlugin extends Plugin {

    private static final String EVENT_PARTIAL = "speechPartial";
    private static final String EVENT_FINAL = "speechFinal";
    private static final String EVENT_ERROR = "speechError";
    private static final String DEFAULT_LANGUAGE = "zh-CN";
    /** 续听重启延迟（ms）：onResults/onError 回调栈内直接 startListening 易 busy
     * （延迟值经验值，语音间隙 200ms 内人耳无感） */
    private static final long RESTART_DELAY_MS = 200L;

    private SpeechRecognizer recognizer;
    private Intent recognitionIntent;
    private boolean sessionActive = false;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    /** 探测系统识别服务可用性（Android 11+ 需 manifest queries 声明 RecognitionService） */
    @PluginMethod
    public void available(PluginCall call) {
        bridge.executeOnMainThread(() -> {
            JSObject data = new JSObject();
            data.put("available", SpeechRecognizer.isRecognitionAvailable(bridge.getContext()));
            call.resolve(data);
        });
    }

    /** 启动识别会话（language 默认 zh-CN；partial 结果实时上报）。busy=已有会话在途。 */
    @PluginMethod
    public void start(PluginCall call) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("activity unavailable");
            return;
        }
        String language = call.getString("language", DEFAULT_LANGUAGE);
        bridge.executeOnMainThread(() -> {
            if (!SpeechRecognizer.isRecognitionAvailable(activity)) {
                call.reject("speech recognition service unavailable");
                return;
            }
            if (recognizer != null) {
                call.reject("speech session busy");
                return;
            }
            try {
                recognitionIntent = buildRecognitionIntent(activity, language);
                recognizer = SpeechRecognizer.createSpeechRecognizer(activity);
                recognizer.setRecognitionListener(recognitionListener);
                sessionActive = true;
                recognizer.startListening(recognitionIntent);
                call.resolve();
            } catch (Exception e) {
                destroyRecognizer();
                call.reject("start speech recognition failed", e);
            }
        });
    }

    /** 优雅停止：停听并等 onResults/onError 落最终结果（前端转写态等此事件回 idle）。 */
    @PluginMethod
    public void stop(PluginCall call) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("activity unavailable");
            return;
        }
        bridge.executeOnMainThread(() -> {
            sessionActive = false;
            mainHandler.removeCallbacksAndMessages(null);
            if (recognizer == null) {
                call.resolve();
                return;
            }
            try {
                recognizer.stopListening();
            } catch (Exception e) {
                // 未在聆听态（重复 stop/服务已死）：不再等结果事件，收尾并通知前端回 idle
                destroyRecognizer();
                notifyErrorCode("aborted");
            }
            call.resolve();
        });
    }

    /** 取消：销毁会话，不再上报任何事件（前端丢弃在途结果）。 */
    @PluginMethod
    public void cancel(PluginCall call) {
        bridge.executeOnMainThread(() -> {
            destroyRecognizer();
            call.resolve();
        });
    }

    private final RecognitionListener recognitionListener = new RecognitionListener() {
        @Override
        public void onReadyForSpeech(Bundle params) {
        }

        @Override
        public void onBeginningOfSpeech() {
        }

        @Override
        public void onRmsChanged(float rmsdB) {
        }

        @Override
        public void onBufferReceived(byte[] buffer) {
        }

        @Override
        public void onEndOfSpeech() {
        }

        @Override
        public void onPartialResults(Bundle partialResults) {
            String text = firstTranscript(partialResults);
            if (!text.isEmpty()) {
                JSObject data = new JSObject();
                data.put("transcript", text);
                notifyListeners(EVENT_PARTIAL, data);
            }
        }

        @Override
        public void onResults(Bundle results) {
            String text = firstTranscript(results);
            if (sessionActive && !text.isEmpty()) {
                JSObject data = new JSObject();
                data.put("transcript", text);
                notifyListeners(EVENT_FINAL, data);
            }
            restartOrDestroy();
        }

        @Override
        public void onError(int error) {
            boolean recoverable = error == SpeechRecognizer.ERROR_NO_MATCH
                    || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT
                    || error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY;
            if (sessionActive && recoverable) {
                // 静音/无匹配/暂态忙：静默续听（用户仍在按住说话），不惊动前端
                restartOrDestroy();
                return;
            }
            notifyErrorCode(mapErrorCode(error));
            destroyRecognizer();
        }

        @Override
        public void onEvent(int eventType, Bundle params) {
        }
    };

    /** 会话仍活跃则延迟续听（用户未松手），否则销毁。 */
    private void restartOrDestroy() {
        if (!sessionActive || recognizer == null || recognitionIntent == null) {
            destroyRecognizer();
            return;
        }
        mainHandler.postDelayed(() -> {
            if (!sessionActive || recognizer == null || recognitionIntent == null) {
                return;
            }
            try {
                recognizer.startListening(recognitionIntent);
            } catch (Exception e) {
                notifyErrorCode("client");
                destroyRecognizer();
            }
        }, RESTART_DELAY_MS);
    }

    private void destroyRecognizer() {
        sessionActive = false;
        mainHandler.removeCallbacksAndMessages(null);
        if (recognizer != null) {
            try {
                recognizer.destroy();
            } catch (Exception ignored) {
                // 服务已死等场景 destroy 再抛错：置空即可
            }
            recognizer = null;
        }
        recognitionIntent = null;
    }

    private static Intent buildRecognitionIntent(Activity activity, String language) {
        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, language);
        intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        intent.putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, activity.getPackageName());
        return intent;
    }

    /** 取首条候选并去首尾空白（partial/final 同构；无文本返回空串由调用方过滤） */
    private static String firstTranscript(Bundle bundle) {
        if (bundle == null) {
            return "";
        }
        ArrayList<String> texts = bundle.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        if (texts == null || texts.isEmpty() || texts.get(0) == null) {
            return "";
        }
        return texts.get(0).trim();
    }

    private void notifyErrorCode(String code) {
        JSObject data = new JSObject();
        data.put("code", code);
        notifyListeners(EVENT_ERROR, data);
    }

    /** Android ERROR_* → Web Speech 同形错误码（前端决策词表，见 frontend nativeSpeech.ts） */
    private static String mapErrorCode(int error) {
        switch (error) {
            case SpeechRecognizer.ERROR_NETWORK_TIMEOUT:
            case SpeechRecognizer.ERROR_NETWORK:
                return "network";
            case SpeechRecognizer.ERROR_AUDIO:
                return "audio-capture";
            case SpeechRecognizer.ERROR_NO_MATCH:
                return "no-match";
            case SpeechRecognizer.ERROR_RECOGNIZER_BUSY:
                return "busy";
            case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                return "not-allowed";
            case SpeechRecognizer.ERROR_SERVER:
                return "server-error";
            case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                return "no-speech";
            case SpeechRecognizer.ERROR_CLIENT:
            default:
                return "client";
        }
    }

    @Override
    protected void handleOnDestroy() {
        Runnable destroy = this::destroyRecognizer;
        Bridge b = bridge;
        if (b != null) {
            b.executeOnMainThread(destroy);
        } else {
            destroy.run();
        }
    }
}
