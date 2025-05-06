/*
 * ESP32 Pet Feeder with Deep Sleep
 * 
 * Fixed version that resolves the following compilation errors:
 * 1. Renamed variable references from calibration_factor1/calibration_factor2 to scale1_calibration_factor/scale2_calibration_factor
 * 2. Corrected function call from checkAndPerformMotorTest to checkAndRunMotorTest
 * 3. Added missing function declaration for testMotorSequence
 * 
 * This code implements a pet feeder system with:
 * - HX711 load cell amplifiers for weight sensing
 * - Deep sleep mode for power efficiency
 * - Non-volatile memory (NVM) storage for calibration and tare values
 * - HTTP communication with a central server
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <HX711.h>
#include <Preferences.h> // Add Preferences library for NVM storage

// Wi-Fi credentials
#define WIFI_SSID "Bouji"
#define WIFI_PASS "nemelok69"

// Server config
const char* serverIP = "https://petfeed.pro";
WiFiClientSecure client;

// HX711 Pins
constexpr byte SCALE1_DT  = 35;
constexpr byte SCALE1_SCK = 27;  // Changed to avoid pin conflicts
constexpr byte SCALE2_DT  = 33;
constexpr byte SCALE2_SCK = 32;

// Motor control pins (HG7881 for now)
constexpr byte MOTOR_IN1 = 25;
constexpr byte MOTOR_IN2 = 26;

// Current sensor pin (ACS712)
constexpr byte CURR_PIN = 34;  // ADC1 to avoid Wi-Fi conflict

// Deep sleep configuration
#define uS_TO_S_FACTOR 1000000  // Conversion factor for micro seconds to seconds
#define DEEP_SLEEP_TIME_SECONDS 30 // 30 seconds for testing (was 300)
RTC_DATA_ATTR int bootCount = 0; // Stored in RTC memory, persists during deep sleep

// Create scale instances
HX711 scale1;
HX711 scale2;

// Create Preferences instance for NVM storage
Preferences preferences;

// Global state variables
bool currentlyFeeding = false;
unsigned long lastProcessedTimestamp = 0;
float scale1_calibration_factor = 430.0; // Default/placeholder
float scale2_calibration_factor = 430.0; // Default/placeholder
long scale1_offset = 0; // Zero offset for scale 1
long scale2_offset = 0; // Zero offset for scale 2

struct FeedRequest {
  bool feed;
  int amount; // in grams
};

// Function declarations
FeedRequest checkFeedRequest();
void connectWiFi();
void postWeights();
bool checkAndPerformTare();
void checkAndRunMotorTest();
void postESP32Status();
void checkAndRestartESP32();
void fetchAndApplyCalibrationFactors();
void saveCalibrationToNVM();
void loadCalibrationFromNVM();
void saveTareToNVM(int scale_id, long offset);
void loadTareFromNVM();
void enterDeepSleep();
void checkAndScheduleFeeding();
float getWeight(int scale_id);
void updateESP32Status();
void testMotorSequence();

// New timer variables
#define WEIGHT_UPDATE_INTERVAL 30000  // 30 seconds between weight updates
#define TASK_CHECK_INTERVAL 5000    // 5 seconds between task checks

// Timers for operations
unsigned long lastWeightUpdate = 0;
unsigned long lastTaskCheck = 0;

void connectWiFi() {
  Serial.printf("Trying SSID: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.disconnect(true, true);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint8_t tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 30) {
    Serial.print(".");
    delay(1000);
    tries++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\n✅ Connected. IP: ");
    Serial.println(WiFi.localIP());
    // Configure client to trust all certificates (not secure but OK for testing)
    client.setInsecure();
  } else {
    Serial.println("\n❌ Wi-Fi failed.");
  }
}

// Function to save calibration factors to NVM
void saveCalibrationToNVM() {
  preferences.putFloat("calib1", scale1_calibration_factor);
  preferences.putFloat("calib2", scale2_calibration_factor);
  Serial.println("✅ Calibration factors saved to NVM");
  Serial.printf("  Scale 1: %.6f\n", scale1_calibration_factor);
  Serial.printf("  Scale 2: %.6f\n", scale2_calibration_factor);
}

// Function to load calibration factors from NVM
void loadCalibrationFromNVM() {
  scale1_calibration_factor = preferences.getFloat("calib1", 430.0);
  scale2_calibration_factor = preferences.getFloat("calib2", 430.0);
  Serial.println("✅ Calibration factors loaded from NVM");
  Serial.printf("  Scale 1: %.6f\n", scale1_calibration_factor);
  Serial.printf("  Scale 2: %.6f\n", scale2_calibration_factor);
  
  // Apply factors to scales
  scale1.set_scale(scale1_calibration_factor);
  scale2.set_scale(scale2_calibration_factor);
}

// Function to save tare offset to NVM
void saveTareToNVM(int scale_id, long offset) {
  if (scale_id == 1) {
    preferences.putLong("tare1", offset);
    scale1_offset = offset;
  } else if (scale_id == 2) {
    preferences.putLong("tare2", offset);
    scale2_offset = offset;
  }
  Serial.printf("✅ Tare offset for Scale %d saved to NVM: %ld\n", scale_id, offset);
}

// Function to load tare offsets from NVM
void loadTareFromNVM() {
  // Load tare offsets with default 0 if they don't exist
  scale1_offset = preferences.getLong("tare1", 0);
  scale2_offset = preferences.getLong("tare2", 0);
  
  Serial.printf("✅ Loaded tare offsets from NVM: Scale1=%ld, Scale2=%ld\n", 
                scale1_offset, scale2_offset);
  
  // Apply the loaded offsets to the scales
  scale1.set_offset(scale1_offset);
  scale2.set_offset(scale2_offset);
}

// Function to enter deep sleep mode
void enterDeepSleep() {
  Serial.println("Entering deep sleep mode for testing...");
  // Close preferences before sleep to ensure data is saved
  preferences.end();
  
  // Configure deep sleep parameters
  esp_sleep_enable_timer_wakeup(DEEP_SLEEP_TIME_SECONDS * uS_TO_S_FACTOR);
  Serial.printf("Device will wake up in %d seconds (TEST MODE - shortened for testing)\n", DEEP_SLEEP_TIME_SECONDS);
  
  // Enter deep sleep
  Serial.println("Going to sleep now...");
  Serial.flush(); 
  esp_deep_sleep_start();
}

// Function to check if feeding is needed and schedule it (REMOVED - Logic handled by server)
void checkAndScheduleFeeding() {
  // This function is no longer used.
  // Feeding is triggered only by checkFeedRequest() from the server.
}

void setup() {
  Serial.begin(115200);
  delay(1000); // Give time for the serial monitor to connect
  
  // Increment boot count and print it
  bootCount++;
  Serial.println("\n=== ESP32 Pet Feeder Starting ===");
  Serial.printf("Boot count: %d\n", bootCount);
  
  // Print wake-up reason for debugging deep sleep
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  Serial.print("Wake-up reason: ");
  switch(wakeup_reason) {
    case ESP_SLEEP_WAKEUP_EXT0:     Serial.println("External RTC_IO"); break;
    case ESP_SLEEP_WAKEUP_EXT1:     Serial.println("External RTC_CNTL"); break;
    case ESP_SLEEP_WAKEUP_TIMER:    Serial.println("Timer (normal deep sleep wake-up)"); break;
    case ESP_SLEEP_WAKEUP_TOUCHPAD: Serial.println("Touchpad"); break;
    case ESP_SLEEP_WAKEUP_ULP:      Serial.println("ULP program"); break;
    default:                        Serial.println("Normal boot (not from deep sleep)"); break;
  }
  Serial.printf("Deep sleep interval set to %d seconds (TEST MODE)\n", DEEP_SLEEP_TIME_SECONDS);

  // Initialize Preferences for NVM storage
  preferences.begin("petfeeder", false); // false = RW mode
  
  // Motor pins - set explicitly to LOW first
  pinMode(MOTOR_IN1, OUTPUT);
  pinMode(MOTOR_IN2, OUTPUT);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
  Serial.println("✅ Motor pins initialized");

  // Initialize HX711 scales with extended initialization time
  Serial.println("⚖️ Initializing scales...");
  scale1.begin(SCALE1_DT, SCALE1_SCK);
  scale2.begin(SCALE2_DT, SCALE2_SCK);
  
  // Give the scales more time to initialize and stabilize
  Serial.println("⏳ Allowing scales to stabilize (3 seconds)...");
  delay(3000);

  // Load calibration and tare values from NVM before using the scales
  loadCalibrationFromNVM();
  loadTareFromNVM();
  
  // DEBUG: Check if scales are responding with multiple readings for stability
  if (scale1.is_ready()) {
    Serial.println("✅ Scale 1 is ready");
    // Take multiple readings
    long total1 = 0;
    int count1 = 0;
    for (int i = 0; i < 5; i++) {
      if (scale1.is_ready()) {
        long reading = scale1.read();
        total1 += reading;
        count1++;
        Serial.printf("  Reading %d: %ld\n", i+1, reading);
        delay(100);
      }
    }
    
    if (count1 > 0) {
      long avg1 = total1 / count1;
      Serial.printf("  Initial raw reading average: %ld\n", avg1);
      float weight1 = (avg1 - scale1_offset) / scale1_calibration_factor;
      Serial.printf("  Initial calculated weight: %.1fg\n", weight1);
    }
  } else {
    Serial.println("❌ Scale 1 is NOT ready - check wiring!");
  }
  
  if (scale2.is_ready()) {
    Serial.println("✅ Scale 2 is ready");
    // Take multiple readings
    long total2 = 0;
    int count2 = 0;
    for (int i = 0; i < 5; i++) {
      if (scale2.is_ready()) {
        long reading = scale2.read();
        total2 += reading;
        count2++;
        Serial.printf("  Reading %d: %ld\n", i+1, reading);
        delay(100);
      }
    }
    
    if (count2 > 0) {
      long avg2 = total2 / count2;
      Serial.printf("  Initial raw reading average: %ld\n", avg2);
      Serial.printf("  Current offset for Scale 2: %ld\n", scale2_offset);
      Serial.printf("  Current calibration factor for Scale 2: %.6f\n", scale2_calibration_factor);
      float weight2 = (avg2 - scale2_offset) / scale2_calibration_factor;
      Serial.printf("  Initial calculated weight: %.1fg\n", weight2);
      
      // Check if calibration factor might be problematic
      if (abs(scale2_calibration_factor) < 1.0) {
        Serial.println("⚠️ WARNING: Scale 2 calibration factor is very small!");
        Serial.println("   This can cause very large weight readings.");
        Serial.println("   Consider recalibrating Scale 2 with a known weight.");
      }
    }
  } else {
    Serial.println("❌ Scale 2 is NOT ready - check wiring!");
  }
  
  Serial.println("✅ Scales initialization complete");

  // Connect to WiFi
  connectWiFi();
  
  // Fetch calibration factors from server
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("📡 Fetching calibration factors from server...");
    fetchAndApplyCalibrationFactors();
    delay(1000);  // Give time to process
    
    // Post initial weight readings
    postWeights();
    delay(1000);  
    
    // Update ESP32 status on server
    updateESP32Status();
    delay(1000);  
    
    // Check for any initial feed requests (e.g., scheduled during sleep)
    FeedRequest initialFeed = checkFeedRequest();
    if(initialFeed.feed){
      Serial.println("✅ Initial feed request found! Starting feed...");
      runMotorWithJamDetectionAndWeight(initialFeed.amount);
      // Note: reportFeedCompletion is called inside runMotor...
    } else {
      Serial.println("ℹ️ No initial feed request found.");
    }
    delay(500); 

    // Check for any motor test requests (like clear jam)
    checkAndRunMotorTest(); 
    delay(500); // Add small delay after potential motor run
    
    // Safeguard to double-check for tare requests before sleep
    Serial.println("🔄 Double-checking for any pending tare requests before sleep...");
    bool doubleTare = checkAndPerformTare(); 
    if (doubleTare) {
      Serial.println("✅ Additional tare request processed during double-check");
    }
    delay(1000);  
  } else {
    Serial.println("❌ WiFi not connected, skipping initial server operations.");
  }
  
  // Initialize time variables
  lastWeightUpdate = millis();
  lastTaskCheck = millis();
  
  // After the initial operations, enter deep sleep
  Serial.println("Initial operations complete, entering deep sleep...");
  delay(1000);  // Ensure serial output is complete
  enterDeepSleep();
}

// The deep sleep version doesn't need the traditional loop()
// All operations are performed in setup(), then device sleeps
void loop() {
  // Use the current time for various operations
  unsigned long currentMillis = millis();
  
  // Check if we need to perform regular tasks
  if (currentMillis - lastTaskCheck > TASK_CHECK_INTERVAL) {
    lastTaskCheck = currentMillis;
    
    // Check for server requests only if WiFi is connected
    if (WiFi.status() == WL_CONNECTED) {
      // Check for tare requests
      checkAndPerformTare();
      
      // Check for motor test requests
      checkAndRunMotorTest();
      
      // Check for ESP32 restart requests
      checkAndRestartESP32();
      
      // Check for feed requests from server
      FeedRequest currentFeed = checkFeedRequest();
      if(currentFeed.feed){
        Serial.println("✅ Feed request found during loop! Starting feed...");
        runMotorWithJamDetectionAndWeight(currentFeed.amount);
        // Note: reportFeedCompletion is called inside runMotor...
      } 
      // No need to check local schedule anymore
      // checkAndScheduleFeeding(); 
    } else {
      // Optional: Attempt to reconnect if WiFi is down
      Serial.println("❌ WiFi disconnected in loop, attempting reconnect...");
      connectWiFi();
    }
  }
  
  // Check if it's time to post weight data then deep sleep
  if (currentMillis - lastWeightUpdate > WEIGHT_UPDATE_INTERVAL) {
    // Read weight values and post to server
    if (WiFi.status() == WL_CONNECTED) {
      postWeights();
      delay(1000);
      updateESP32Status();
    } else {
      Serial.println("❌ WiFi disconnected, cannot post weight/status.");
    }
    
    // Create a brief window to allow tare requests to be received before sleep
    Serial.println("⏳ Waiting briefly for any pending tare requests before sleep...");
    unsigned long waitStartTime = millis();
    while (millis() - waitStartTime < 3000) {
      if (WiFi.status() == WL_CONNECTED && checkAndPerformTare()) {
        Serial.println("✅ Tare request processed during wait period");
        break;
      }
      delay(200);
    }
    
    // Final check for tare requests
    if (WiFi.status() == WL_CONNECTED) {
       checkAndPerformTare();
       delay(500); 
    }
    
    // Enter deep sleep
    enterDeepSleep();
  }
}

float readCurrentA() {
  int raw = analogRead(CURR_PIN);
  float voltage = raw * (3.3 / 4095.0);
  float current = (voltage - 2.5) / 0.185;  // ACS712 5A version
  return current;
}

void fetchAndApplyCalibrationFactors(){
  if (WiFi.status() != WL_CONNECTED) return;
  
  Serial.println("\n🔍 Fetching calibration factors...");
  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000);
  https.begin(client, String(serverIP) + "/calibration-factors");
  int code = https.GET();
  String response = "";
  if (code > 0) {
      response = https.getString();
  }
  Serial.printf("📡 Calibration factor response code: %d\n", code);
  Serial.printf("📄 Calibration factor response body: '%s'\n", response.c_str());

  if (code == 200) {
    // Basic parsing - assumes format {"1": factor1, "2": factor2}
    int idx1 = response.indexOf("\"1\":");
    int idx2 = response.indexOf("\"2\":");

    if (idx1 != -1 && idx2 != -1) {
        int start1 = idx1 + 4;
        int end1 = response.indexOf(",", start1);
        if (end1 == -1) end1 = response.indexOf("}", start1);
        String factor1Str = response.substring(start1, end1);
        factor1Str.trim();
        float factor1 = factor1Str.toFloat();

        int start2 = idx2 + 4;
        int end2 = response.indexOf("}", start2);
        String factor2Str = response.substring(start2, end2);
        factor2Str.trim();
        float factor2 = factor2Str.toFloat();

        // Log the parsed values
        Serial.printf("📊 Parsed calibration factors: Scale1=%.6f, Scale2=%.6f\n", factor1, factor2);
        
        // Perform sanity check on factors - extremely small values will cause large weights
        bool factor1Valid = (factor1 != 0 && abs(factor1) >= 10.0);
        bool factor2Valid = (factor2 != 0 && abs(factor2) >= 10.0);
        
        if (!factor1Valid) {
          Serial.printf("⚠️ Scale 1 factor (%.6f) is invalid or too small! Keeping current value.\n", factor1);
          factor1 = scale1_calibration_factor; // Keep current value
        }
        
        if (!factor2Valid) {
          Serial.printf("⚠️ Scale 2 factor (%.6f) is invalid or too small! Keeping current value.\n", factor2);
          factor2 = scale2_calibration_factor; // Keep current value
          
          // If current value is also problematic, use a safe default
          if (abs(factor2) < 10.0) {
            Serial.println("⚠️ Current Scale 2 factor is also problematic! Using safe default.");
            factor2 = 430.0; // Safe default value
          }
        }
        
        // Now proceed with valid factors
        bool factor1Changed = abs(factor1 - scale1_calibration_factor) > 0.01;
        bool factor2Changed = abs(factor2 - scale2_calibration_factor) > 0.01;

        if (factor1Changed || factor2Changed) {
            Serial.printf("✅ Applying NEW factors: Scale1=%.6f, Scale2=%.6f\n", factor1, factor2);
            
            // Store old values for comparison
            float old_factor1 = scale1_calibration_factor;
            float old_factor2 = scale2_calibration_factor;
            
            // Apply new values
            scale1_calibration_factor = factor1;
            scale2_calibration_factor = factor2;
            
            // Apply factors to scales
            scale1.set_scale(scale1_calibration_factor);
            scale2.set_scale(scale2_calibration_factor);
            
            Serial.println("✅ Calibration factors applied to scales.");
            Serial.printf("   Scale 1: %.6f (was %.6f)\n", factor1, old_factor1);
            Serial.printf("   Scale 2: %.6f (was %.6f)\n", factor2, old_factor2);
            
            // Save the new factors to NVM
            saveCalibrationToNVM();
            
            // Check the impact on current readings
            if (scale1.is_ready() && scale2.is_ready()) {
              long raw1 = scale1.read();
              long raw2 = scale2.read();
              
              float weight1_new = (raw1 - scale1_offset) / scale1_calibration_factor;
              float weight1_old = (raw1 - scale1_offset) / old_factor1;
              
              float weight2_new = (raw2 - scale2_offset) / scale2_calibration_factor;
              float weight2_old = (raw2 - scale2_offset) / old_factor2;
              
              Serial.println("📊 Impact on current readings:");
              Serial.printf("   Scale 1: %.1fg (was %.1fg)\n", weight1_new, weight1_old);
              Serial.printf("   Scale 2: %.1fg (was %.1fg)\n", weight2_new, weight2_old);
            }
            
            Serial.println("ℹ️ NOTE: Scales NOT automatically re-tared. Use manual tare if needed.");
        } else {
            Serial.println("ℹ️ Fetched factors are same as current, no change applied.");
        }
    } else {
        Serial.println("❌ Could not find factors in response.");
    }
  } else {
      Serial.println("❌ Failed to fetch calibration factors.");
  }
  https.end();
}

// Function to get weight from a scale with error handling
float getWeight(int scale_id) {
  HX711 &scale = (scale_id == 1) ? scale1 : scale2;
  long offset = (scale_id == 1) ? scale1_offset : scale2_offset;
  float calibration_factor = (scale_id == 1) ? scale1_calibration_factor : scale2_calibration_factor;
  
  // Check if scale is ready
  if (!scale.is_ready()) {
    Serial.printf("⚠️ Scale %d not ready during weight reading\n", scale_id);
    return -999.0; // Error indicator
  }

  // Take multiple readings for stability
  const int NUM_READINGS = 5;
  long readings[NUM_READINGS];
  int valid_readings = 0;
  long total = 0;
  
  // Collect readings
  for (int i = 0; i < NUM_READINGS; i++) {
    if (scale.is_ready()) {
      readings[i] = scale.read();
      total += readings[i];
      valid_readings++;
      delay(100); // Short delay between readings
    } else {
      Serial.printf("⚠️ Scale %d reading %d failed\n", scale_id, i+1);
    }
  }

  // Check if we got enough valid readings
  if (valid_readings < 3) { // Need at least 3 valid readings
    Serial.printf("❌ Scale %d: Not enough valid readings (%d/%d)\n", 
                 scale_id, valid_readings, NUM_READINGS);
    return -999.0;
  }
  
  // Calculate average reading
  long avg_reading = total / valid_readings;
  
  // Extended debug information
  Serial.printf("🔍 Scale %d DETAILED: Raw=%ld, Offset=%ld, Factor=%.6f\n", 
               scale_id, avg_reading, offset, calibration_factor);
               
  // Check for problematic calibration factor
  if (abs(calibration_factor) < 10.0) {
    Serial.printf("⚠️ Scale %d has very small calibration factor (%.6f)! Using safe default.\n", 
                 scale_id, calibration_factor);
    // Use a safe default to prevent division by near-zero
    calibration_factor = 430.0;
  }
  
  // Calculate weight using calibration factor
  float weight;
  if (calibration_factor == 0) {
    Serial.printf("❌ Scale %d calibration factor is ZERO! Cannot calculate weight.\n", scale_id);
    return -999.0;
  } else {
    weight = (float)(avg_reading - offset) / calibration_factor;
  }
  
  // Advanced debugging for calculations
  Serial.printf("📊 Scale %d CALC: (%ld - %ld) / %.6f = %.1fg\n", 
               scale_id, avg_reading, offset, calibration_factor, weight);
               
  // Limit extremely large readings which are likely erroneous
  if (abs(weight) > 10000.0) {
    Serial.printf("⚠️ Scale %d weight (%.1f) is unreasonably large, likely a calculation error\n", 
                 scale_id, weight);
    return -999.0;
  }
  
  return weight;
}

// Post weight data to server
void postWeights() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("❌ WiFi not connected for weight posting");
    return;
  }

  // Get weight readings with error handling
  float weight1 = getWeight(1);
  float weight2 = getWeight(2);
  
  // Get raw values (average of 3 readings for stability)
  long raw1 = 0, raw2 = 0;
  int count1 = 0, count2 = 0;
  
  for (int i = 0; i < 3; i++) {
    if (scale1.is_ready()) {
      raw1 += scale1.read();
      count1++;
    }
    if (scale2.is_ready()) {
      raw2 += scale2.read();
      count2++;
    }
    delay(100);
  }
  
  // Calculate averages or use default values if no readings
  raw1 = (count1 > 0) ? (raw1 / count1) : 0;
  raw2 = (count2 > 0) ? (raw2 / count2) : 0;

  // Create URL with data
  String url = String(serverIP) + "/scales";
  
  // Create JSON payload
  String payload = "{";
  
  // Only include valid weight readings
  if (weight1 > -999.0) {
    payload += "\"scale1\":" + String(weight1, 1) + ",";
    payload += "\"scale1_raw\":" + String(raw1);
  } else {
    payload += "\"scale1\":null,";
    payload += "\"scale1_raw\":" + String(raw1);
  }
  
  payload += ",";
  
  if (weight2 > -999.0) {
    payload += "\"scale2\":" + String(weight2, 1) + ",";
    payload += "\"scale2_raw\":" + String(raw2);
  } else {
    payload += "\"scale2\":null,";
    payload += "\"scale2_raw\":" + String(raw2);
  }
  
  payload += "}";
  
  Serial.println("📊 Posting weight data to server: " + url);
  Serial.println("  Payload: " + payload);

  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  
  int httpResponseCode = http.POST(payload);
  if (httpResponseCode > 0) {
    String response = http.getString();
    Serial.printf("✅ HTTP Response code: %d\n", httpResponseCode);
    Serial.println("  Response: " + response);
  } else {
    Serial.printf("❌ Error on sending POST: %d\n", httpResponseCode);
  }
  
  http.end();
}

FeedRequest checkFeedRequest() {
  FeedRequest req = {false, 0};
  
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("❌ WiFi not connected, skipping feed check");
    return req;
  }

  Serial.printf("\n🔍 Checking feed request at %s/check-feed-request\n", serverIP);

  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000);
  https.begin(client, String(serverIP) + "/check-feed-request");
  int code = https.GET();
  String response = https.getString();
  
  Serial.printf("📡 Response code: %d\n", code);
  Serial.printf("📄 Raw response [%d bytes]: '%s'\n", response.length(), response.c_str());
  
  if (code == 200) {
    // Find the feed field
    int feedIndex = response.indexOf("\"feed\":");
    if(feedIndex != -1) {
      int valueStart = feedIndex + 7; // length of "feed": is 7
      String feedValue = response.substring(valueStart);
      feedValue.trim();

      bool feedFound = false;
      if(feedValue.startsWith("true")) {
        feedFound = true;
      } else if(feedValue.startsWith("false")) {
        // If server explicitly says false, reset our state if we thought we were feeding
        if (currentlyFeeding) {
            Serial.println("ℹ️ Feed command is FALSE from server, resetting state.");
            currentlyFeeding = false;
        }
      } else {
        Serial.printf("⚠️ Unexpected feed value: %s\n", feedValue.c_str());
      }

      if (feedFound) {
          Serial.println("✅ Feed command is TRUE from server.");
          // Look for timestamp
          unsigned long timestamp = 0;
          int timestampIndex = response.indexOf("\"timestamp\":");
          if(timestampIndex != -1) {
            int tsStart = timestampIndex + 12; // length of "timestamp": is 12
            int tsEnd = response.indexOf(",", tsStart);
            if(tsEnd == -1) tsEnd = response.indexOf("}", tsStart);
            if(tsEnd != -1) {
              String tsStr = response.substring(tsStart, tsEnd);
              tsStr.trim();
              // Use strtoul for potentially large unsigned long timestamps
              timestamp = strtoul(tsStr.c_str(), NULL, 10);
              Serial.printf("Request timestamp found: %lu, Last processed: %lu\n", 
                           timestamp, lastProcessedTimestamp);
            } else {
                 Serial.println("⚠️ Could not parse timestamp value.");
            }
          } else {
               Serial.println("⚠️ Timestamp field not found in response.");
          }

          // Only process if timestamp is newer and we aren't already feeding
          if(timestamp > 0 && timestamp > lastProcessedTimestamp) {
              if (!currentlyFeeding) {
                  // Look for amount
                  int amountIndex = response.indexOf("\"amount\":");
                  if(amountIndex != -1) {
                      int valueStart = amountIndex + 9; // length of "amount": is 9
                      int valueEnd = response.indexOf("}", valueStart);
                      if(valueEnd == -1) valueEnd = response.indexOf(",", valueStart);
                      if(valueEnd == -1) valueEnd = response.length();
                      
                      String amountStr = response.substring(valueStart, valueEnd);
                      amountStr.trim();
                      // Remove quotes if amount is sent as string "50g"
                      amountStr.replace("\"", ""); 
                      amountStr.replace("g", ""); // Remove 'g' if present
                      Serial.printf("Amount string found: '%s'\n", amountStr.c_str());
                      
                      int amountValue = amountStr.toInt();
                      if(amountValue > 0) {
                          req.amount = amountValue;
                          req.feed = true;
                          currentlyFeeding = true; // Set state to feeding
                          lastProcessedTimestamp = timestamp; // Update last processed timestamp
                          Serial.printf("✅ Processing NEW feed request for %d grams (Timestamp: %lu)\n", 
                                      amountValue, timestamp);
                      } else {
                          Serial.println("❌ Invalid or zero amount parsed.");
                      }
                  } else {
                      Serial.println("❌ No amount field found in feed request.");
                  }
              } else {
                  Serial.println("⏳ Already feeding, ignoring NEW request check (will finish current first).");
              }
          } else {
              Serial.printf("⏭️ Skipping request: Timestamp %lu <= %lu or zero.\n", 
                           timestamp, lastProcessedTimestamp);
          }
      }
    } else {
      Serial.println("❌ No 'feed' field found in response.");
    }
  } else {
    Serial.printf("❌ Failed to get feed request, code: %d\n", code);
  }
  https.end();

  // Debug final decision state
  Serial.println("\nFinal request state for this check:");
  Serial.printf("- Feed Flag to Return: %d\n", req.feed);
  Serial.printf("- Amount to Return: %d\n", req.amount);
  Serial.printf("- Currently Feeding State: %d\n", currentlyFeeding);
  Serial.printf("- Last Processed Timestamp: %lu\n", lastProcessedTimestamp);
  
  return req;
}

// Function to report completion back to server
void reportFeedCompletion(String status, String notes = "", float final_dispensed_amount = -1.0) {
  if (WiFi.status() != WL_CONNECTED) return;

  // We are no longer feeding after this report
  currentlyFeeding = false; 

  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000);
  https.begin(client, String(serverIP) + "/report-feed-complete");
  https.addHeader("Content-Type", "application/json");
  
  // Include final status, notes, and the actual dispensed amount
  String json = String("{\"status\":\"") + status + 
                "\", \"notes\":\"" + notes + 
                "\", \"final_amount\":" + String(final_dispensed_amount, 1) + // Send with 1 decimal place
                "}";
  
  Serial.printf("Reporting feed completion to server: %s\n", json.c_str());
  int code = https.POST(json);
  String response = https.getString();
  Serial.printf("Completion report response: %d - %s\n", code, response.c_str());
  
  https.end();
}

void runMotorWithJamDetectionAndWeight(int targetGrams) {
  Serial.printf("\n=== Starting motor sequence for %d grams (using Scale 1 - Bowl, Pause/Read) ===\n", targetGrams);
  bool success = true; // Assume success unless set otherwise
  String completionNotes = "";
  float totalDispensed = 0.0; // Initialize
  float initialWeight = 0.0;  // Initialize

  // Check Scale 1 readiness first
  if (!scale1.is_ready()) {
      Serial.println("❌ Scale 1 (Bowl Scale) not ready!");
      success = false;
      completionNotes = "scale1_error";
      // Go directly to report completion without running motor
      reportFeedCompletion(success ? "success" : "error", completionNotes, totalDispensed); 
      return; 
  }

  // Get initial weight only if scale is ready
  initialWeight = scale1.get_units(10); // Use Scale 1
  Serial.printf("Initial weight (Scale 1 - Bowl): %.1fg\n", initialWeight);
  
  int stopEarly = 6; 
  float targetWeight = initialWeight + targetGrams - stopEarly;
  Serial.printf("Target weight (Scale 1 - Bowl): %.1fg (stopping %dg early)\n", targetWeight, stopEarly);

  // Dispensing sequence
  unsigned long loopStart = millis(); 
  bool jamDetected = false;
  float currentWeight = initialWeight;
  const unsigned long motorPulseDuration = 200; 
  const unsigned long settleTime = 300;       

  if (currentWeight < targetWeight) { // Only run loop if needed
      // Dispensing loop
      while (true) {
          // --- Run Motor --- 
          Serial.print("🔄 Motor ON...");
          digitalWrite(MOTOR_IN1, HIGH);
          digitalWrite(MOTOR_IN2, LOW);
          delay(motorPulseDuration);
          digitalWrite(MOTOR_IN1, LOW);
          digitalWrite(MOTOR_IN2, LOW);
          Serial.println("⏹️ Motor OFF");

          // --- Wait for settle --- 
          Serial.print("⏳ Settling...");
          delay(settleTime);
          Serial.println(" Done.");

          // --- Check Current (briefly check after pulse) ---
          float current = readCurrentA(); 
          Serial.printf("⚡ Current Check: %.2fA\n", current);
          if (current > 0.6) {  
              Serial.println("⚠️ Current spike detected post-pulse! (Possible Jam)");
              jamDetected = true;
              completionNotes = "Current spike detected post-pulse";
              success = false;
              break;
          }

          // --- Check Weight (Use Scale 1) --- 
          currentWeight = scale1.get_units(5); // Use Scale 1
          Serial.printf("⚖️ Current Weight (Scale 1 - Bowl): %.1fg | Target: %.1fg\n", 
                       currentWeight, targetWeight);

          // Check if target weight is reached
          if (currentWeight >= targetWeight) {
              Serial.println("✅ Target weight reached (Scale 1 - Bowl)");
              break;
          }

          // Break conditions
          if (!success || currentWeight >= targetWeight) {
              break; // Exit if error detected or target reached
          }

          // Timeout Check
          if (millis() - loopStart > 25000) { 
              Serial.println("⚠️ Timeout reached during pulsed dispensing");
              jamDetected = true; 
              completionNotes = "Timeout reached (pulsed)";
              success = false;
              break;
          }
      }
      Serial.println("Dispensing loop finished.");
  } else {
      Serial.println("✅ Target weight already met or exceeded at start (Scale 1 - Bowl).");
      success = true; // Mark as success if already met
      completionNotes = "Already at or above target weight.";
  }

  // --- Finalization --- 
  Serial.println("📊 Performing final weight check (Scale 1 - Bowl)...");
  delay(500); 
  float finalWeight = scale1.get_units(10); // Use Scale 1
  totalDispensed = finalWeight - initialWeight; // Calculate final dispensed amount
  Serial.printf("📊 Feed complete. Final Weight (Scale 1 - Bowl): %.1fg, Total Dispensed: %.1fg\n", finalWeight, totalDispensed);

  // Reverse motor if a jam was specifically detected during the loop
  if (jamDetected && completionNotes.indexOf("Current spike") != -1) { // Only reverse on current spike jam
      Serial.println("↩️ Reversing motor briefly...");
      digitalWrite(MOTOR_IN1, LOW);
      digitalWrite(MOTOR_IN2, HIGH);
      delay(500);
      digitalWrite(MOTOR_IN1, LOW);
      digitalWrite(MOTOR_IN2, LOW);
  }
  
  // Update completion notes if it was successful
  if (success) {
      completionNotes = String("Dispensed approx: ") + String(totalDispensed, 1) + "g (Scale 1 - Bowl, Pulsed)";
  }

  Serial.println("=== Motor sequence complete ===\n");

  // Report completion status - THIS WILL NOW ALWAYS RUN
  reportFeedCompletion(success ? "success" : "error", completionNotes, totalDispensed);
}

// Test function that can be called from setup or via server
void testMotorSequence() {
  Serial.println("\n=== Testing motor sequence ===");
  
  // Make sure motors are stopped
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
  delay(1000);
  
  // Forward
  Serial.println("Forward 2 seconds");
  digitalWrite(MOTOR_IN1, HIGH);
  digitalWrite(MOTOR_IN2, LOW);
  delay(2000);
  
  // Stop
  Serial.println("Stop 1 second");
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
  delay(1000);
  
  // Reverse
  Serial.println("Reverse 1 second");
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, HIGH);
  delay(1000);
  
  // Final stop
  Serial.println("Final stop");
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
  
  Serial.println("=== Motor test complete ===\n");
}

bool checkAndPerformTare() {
  if (WiFi.status() != WL_CONNECTED) return false;

  Serial.println("\n🔍 Checking for tare requests...");
  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000);
  https.begin(client, String(serverIP) + "/check-tare-request");
  int code = https.GET();
  String response = "";
  if (code > 0) {
      response = https.getString();
  }
  Serial.printf("📡 Tare check response code: %d\n", code);
  Serial.printf("📄 Tare check response body: '%s'\n", response.c_str());
  
  bool tarePerformed = false;
  
  if (code == 200) {
    // Check specific strings, handling potential variations
    bool tare1Requested = response.indexOf("\"tare1\":true") != -1 || 
                         response.indexOf("\"tare1\": true") != -1;
    bool tare2Requested = response.indexOf("\"tare2\":true") != -1 || 
                         response.indexOf("\"tare2\": true") != -1;
                         
    Serial.printf("Parsed Tare Request: Scale1=%d, Scale2=%d\n", tare1Requested, tare2Requested);

    if (tare1Requested) {
      // First read the current raw value for logging
      long raw_before = 0;
      if (scale1.is_ready()) {
        raw_before = scale1.read();
        Serial.printf("⚖️ Scale 1 raw value before tare: %ld\n", raw_before);
      }
      
      Serial.println("⚖️ Taring Scale 1...");
      scale1.tare(5); // Use 5 readings for more stable tare
      
      // Get and save the new offset to NVM
      long new_offset = scale1.get_offset();
      scale1_offset = new_offset; // Update global variable
      saveTareToNVM(1, new_offset);
      
      // Verify the offset was applied correctly
      long raw_after = 0;
      if (scale1.is_ready()) {
        raw_after = scale1.read();
        Serial.printf("⚖️ Scale 1 raw value after tare: %ld (should be close to offset)\n", raw_after);
        Serial.printf("⚖️ Scale 1 calculated weight after tare: %.1fg (should be near zero)\n", 
                     (float)(raw_after - new_offset) / scale1_calibration_factor);
      }
      
      Serial.printf("✅ Scale 1 tared and offset %ld saved to NVM.\n", new_offset);
      delay(100); // Small delay after tare
      tarePerformed = true;
    }
    
    if (tare2Requested) {
      // First read the current raw value for logging
      long raw_before = 0;
      if (scale2.is_ready()) {
        raw_before = scale2.read();
        Serial.printf("⚖️ Scale 2 raw value before tare: %ld\n", raw_before);
      }
      
      Serial.println("⚖️ Taring Scale 2...");
      scale2.tare(5); // Use 5 readings for more stable tare
      
      // Get and save the new offset to NVM
      long new_offset = scale2.get_offset();
      scale2_offset = new_offset; // Update global variable
      saveTareToNVM(2, new_offset);
      
      // Verify the offset was applied correctly
      long raw_after = 0;
      if (scale2.is_ready()) {
        raw_after = scale2.read();
        Serial.printf("⚖️ Scale 2 raw value after tare: %ld (should be close to offset)\n", raw_after);
        Serial.printf("⚖️ Scale 2 calculated weight after tare: %.1fg (should be near zero)\n", 
                     (float)(raw_after - new_offset) / scale2_calibration_factor);
      }
      
      Serial.printf("✅ Scale 2 tared and offset %ld saved to NVM.\n", new_offset);
      delay(100); // Small delay after tare
      tarePerformed = true;
    }
  } else {
    Serial.println("❌ Failed to get tare request.");
  }
  https.end();
  return tarePerformed;
}

void checkAndRunMotorTest() {
  if (WiFi.status() != WL_CONNECTED) return;
  
  Serial.println("\n🔍 Checking motor test request...");
  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000); 
  https.begin(client, String(serverIP) + "/check-motor-test");
  int code = https.GET();
  String response = "";
  if (code > 0) {
      response = https.getString();
  }
  
  Serial.printf("📡 Motor test check response code: %d\n", code);
  Serial.printf("📄 Motor test check response body: '%s'\n", response.c_str());
  
  if (code == 200) {
    bool testRequested = response.indexOf("\"test\":true") != -1 || 
                        response.indexOf("\"test\": true") != -1;
    Serial.printf("Parsed Motor Test Request: %d\n", testRequested);
    
    if (testRequested) {
      String dir = "forward"; // Default
      if (response.indexOf("\"direction\":\"reverse\"") != -1) {
          dir = "reverse";
      }
      Serial.printf("Parsed Direction: %s\n", dir.c_str());
      
      int duration = 2; // Default
      int durIdx = response.indexOf("\"duration\":");
      if (durIdx != -1) {
        int start = response.indexOf(":", durIdx) + 1;
        int end = response.indexOf("}", start);
        if (end == -1) end = response.indexOf(",", start);
        if (end != -1) {
            String durStr = response.substring(start, end);
            durStr.trim();
            duration = durStr.toInt();
        } else {
            Serial.println("Could not parse duration end.");
        }
      }
      Serial.printf("Parsed Duration: %d seconds\n", duration);
      
      if (duration <= 0 || duration > 10) {
           Serial.println("Invalid duration received, defaulting to 2s.");
           duration = 2;
      }

      Serial.printf("🚦 Running motor test: %s for %d seconds\n", dir.c_str(), duration);
      if (dir == "forward") {
        digitalWrite(MOTOR_IN1, HIGH);
        digitalWrite(MOTOR_IN2, LOW);
      } else { // reverse
        digitalWrite(MOTOR_IN1, LOW);
        digitalWrite(MOTOR_IN2, HIGH);
      }
      delay(duration * 1000);
      digitalWrite(MOTOR_IN1, LOW);
      digitalWrite(MOTOR_IN2, LOW);
      Serial.println("✅ Motor test complete.");
    }
  } else {
      Serial.println("❌ Failed to get motor test request.");
  }
  https.end();
}

void postESP32Status() {
  if (WiFi.status() != WL_CONNECTED) return;
  
  // Get current scale values
  float weight1_grams = scale1.get_units(1);
  float weight2_grams = scale2.get_units(1);
  
  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000); 
  https.begin(client, String(serverIP) + "/esp32-status-update");
  https.addHeader("Content-Type", "application/json");
  String ip = WiFi.localIP().toString();
  int rssi = WiFi.RSSI();
  String firmware = "1.0.1"; // Updated firmware version
  
  // Include scale values in status update
  String json = String("{\"ip\":\"") + ip + 
                "\",\"rssi\":" + rssi + 
                ",\"firmware\":\"" + firmware + "\"" +
                ",\"scale1\":" + String(weight1_grams, 1) +
                ",\"scale2\":" + String(weight2_grams, 1) +
                "}";
  
  https.POST(json);
  https.end();
}

void checkAndRestartESP32() {
  if (WiFi.status() != WL_CONNECTED) return;
  
  Serial.println("\n🔍 Checking for restart request...");
  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000); 
  https.begin(client, String(serverIP) + "/check-esp32-restart");
  int code = https.GET();
  if (code == 200) {
    String response = https.getString();
    if (response.indexOf("\"restart\": true") != -1 || response.indexOf("\"restart\":true") != -1) {
      Serial.println("Restart requested from server. Restarting now...");
      delay(1000);
      ESP.restart();
    }
  }
  https.end();
}

// Function to update ESP32 status on the server
void updateESP32Status() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("❌ WiFi not connected for ESP32 status update");
    return;
  }
  
  String url = String(serverIP) + "/esp32-status-update";
  String payload = "{";
  payload += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  payload += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  payload += "\"firmware\":\"" + String("1.0.0-deepsleep") + "\",";
  payload += "\"boot_count\":" + String(bootCount) + ",";
  payload += "\"free_heap\":" + String(ESP.getFreeHeap());
  payload += "}";
  
  Serial.println("📡 Updating ESP32 status on server");
  Serial.println("  URL: " + url);
  Serial.println("  Payload: " + payload);
  
  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  
  int httpResponseCode = http.POST(payload);
  if (httpResponseCode > 0) {
    String response = http.getString();
    Serial.printf("✅ HTTP Response code: %d\n", httpResponseCode);
    Serial.println("  Response: " + response);
  } else {
    Serial.printf("❌ Error on sending status update: %d\n", httpResponseCode);
  }
  
  http.end();
}
