package com.aurora.markets;

import android.content.DialogInterface;
import android.widget.EditText;

/** Dialog "connect" button: sends the typed address to the activity. */
final class ConnectClick implements DialogInterface.OnClickListener {
    private final MainActivity activity;
    private final EditText input;

    ConnectClick(MainActivity activity, EditText input) {
        this.activity = activity;
        this.input = input;
    }

    @Override
    public void onClick(DialogInterface d, int which) {
        activity.load(input.getText().toString().trim());
    }
}
