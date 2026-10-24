package com.aurora.markets;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.EditText;
import android.widget.FrameLayout;

/**
 * A thin WebView shell around the Aurora Markets web app.
 *
 * Design notes:
 *  - The app is the same server-rendered SPA that runs in the browser, so there
 *    is no second implementation to keep in sync. Sessions live in the server's
 *    cookies; this shell stores no credentials.
 *  - On first launch the user types the server address and it is remembered. A
 *    fixed server can be baked in by setting DEFAULT_URL below.
 *  - Only the configured origin is allowed inside the WebView; any other link
 *    opens in the system browser, so a stray tap cannot navigate away from the
 *    trusted host.
 *  - The camera is granted to the page only so the built-in QR scanner works;
 *    image uploads go through the system file picker.
 *
 * The WebView client/chrome helpers live in their own files (NavClient,
 * ChromeHost, ConnectClick) rather than as nested classes: the d8 in this build
 * toolchain crashes on any InnerClasses attribute, and separate top-level
 * classes also keep each file small enough to read in one go.
 */
public class MainActivity extends Activity {

    /** Set this to your server to skip the first-run prompt. */
    public static final String DEFAULT_URL = BuildConfig.DEFAULT_SERVER;

    public static final String PREFS = "aurora";
    public static final String KEY_URL = "server_url";
    public static final int REQ_FILE = 4243;

    WebView web;
    String baseUrl = "";
    android.webkit.ValueCallback<Uri[]> filePathCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor("#0a0b12"));
        w.setNavigationBarColor(Color.parseColor("#0a0b12"));

        FrameLayout root = new FrameLayout(this);
        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.setBackgroundColor(Color.parseColor("#0a0b12"));
        setContentView(root);

        baseUrl = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_URL, DEFAULT_URL);

        web = new WebView(this);
        web.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        web.setBackgroundColor(Color.parseColor("#0a0b12"));
        root.addView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportZoom(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        CookieManager.getInstance().setAcceptCookie(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        }

        web.setWebViewClient(new NavClient(this));
        web.setWebChromeClient(new ChromeHost(this));

        if (baseUrl == null || baseUrl.isEmpty()) {
            promptForServer();
        } else {
            load(baseUrl);
        }
    }

    /** Keep navigation inside the configured origin; everything else -> browser. */
    boolean handleUrl(String url) {
        if (url == null) return false;
        if (url.startsWith("http://") || url.startsWith("https://")) {
            if (baseUrl != null && !baseUrl.isEmpty() && url.startsWith(origin(baseUrl))) {
                return false; // load in-app
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            } catch (Exception ignored) { }
            return true;
        }
        return false; // data:, blob:, about: stay in the WebView
    }

    static String origin(String u) {
        try {
            Uri p = Uri.parse(u);
            return p.getScheme() + "://" + p.getHost() + (p.getPort() > 0 ? ":" + p.getPort() : "");
        } catch (Exception e) {
            return u;
        }
    }

    void load(String url) {
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "https://" + url;
        }
        baseUrl = url;
        getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(KEY_URL, url).apply();
        web.loadUrl(url);
    }

    private void promptForServer() {
        final EditText input = new EditText(this);
        input.setHint("https://your-server.example");
        input.setText("https://");
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(pad, pad, pad, pad);
        box.addView(input);

        new AlertDialog.Builder(this)
                .setTitle("عنوان السيرفر")
                .setMessage("أدخل رابط منصّة Aurora Markets.")
                .setView(box)
                .setPositiveButton("اتصال", new ConnectClick(this, input))
                .setCancelable(false)
                .show();
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE) {
            if (filePathCallback != null) {
                Uri[] out = null;
                if (res == RESULT_OK && data != null && data.getData() != null) {
                    out = new Uri[]{data.getData()};
                }
                filePathCallback.onReceiveValue(out);
                filePathCallback = null;
            }
            return;
        }
        super.onActivityResult(req, res, data);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
