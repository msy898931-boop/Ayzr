package com.aurora.markets;

import android.content.Intent;
import android.net.Uri;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebView;

/**
 * Chrome bits the SPA needs: the camera for the in-page QR scanner and a file
 * picker for chart uploads. Everything else the page asks for is denied.
 */
final class ChromeHost extends WebChromeClient {
    private final MainActivity activity;

    ChromeHost(MainActivity activity) {
        this.activity = activity;
    }

    @Override
    public void onPermissionRequest(final PermissionRequest request) {
        for (String r : request.getResources()) {
            if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                request.grant(new String[]{r});
                return;
            }
        }
        request.deny();
    }

    @Override
    public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
        activity.filePathCallback = cb;
        Intent i = new Intent(Intent.ACTION_GET_CONTENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("image/*");
        try {
            activity.startActivityForResult(Intent.createChooser(i, "اختر صورة"), MainActivity.REQ_FILE);
        } catch (Exception e) {
            activity.filePathCallback = null;
            return false;
        }
        return true;
    }
}
