package com.aurora.markets;

import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/** URL routing: same origin stays in-app, anything else opens in the browser. */
final class NavClient extends WebViewClient {
    private final MainActivity activity;

    NavClient(MainActivity activity) {
        this.activity = activity;
    }

    @Override
    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
        return activity.handleUrl(req.getUrl().toString());
    }

    @Override
    public boolean shouldOverrideUrlLoading(WebView v, String url) {
        return activity.handleUrl(url);
    }
}
