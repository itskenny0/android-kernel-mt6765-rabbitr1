// SPDX-License-Identifier: Apache-2.0
#include "choice.h"
#include <android-base/properties.h>
#include <jni.h>
#include <string>

extern "C" JNIEXPORT jstring JNICALL
Java_cat_kenny_r1_charging_ChargingActivity_getChoice(JNIEnv* env, jclass) {
    const std::string choice = android::base::GetProperty(r1::kProperty, "auto");
    return env->NewStringUTF(r1::CurrentLimit(choice) < 0 ? "500" : choice.c_str());
}

extern "C" JNIEXPORT jboolean JNICALL
Java_cat_kenny_r1_charging_ChargingActivity_setChoice(JNIEnv* env, jclass, jstring input) {
    if (!input) return JNI_FALSE;
    const char* utf = env->GetStringUTFChars(input, nullptr);
    if (!utf) return JNI_FALSE;
    const std::string choice(utf);
    env->ReleaseStringUTFChars(input, utf);
    return r1::CurrentLimit(choice) >= 0 && android::base::SetProperty(r1::kProperty, choice);
}
