#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <ctime>
#include <thread>
#include <atomic>
#include <mutex> 
#include <chrono>
#include <opencv2/opencv.hpp>
#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <zbar.h>
#include <wiringPi.h>
#include <termios.h>
#include <unistd.h>
#include <fcntl.h>
#include <cstdlib> 

// CONFIGURAÇÃO
const std::string MODULO_ID = "bancada_tomates_01";
std::string SERVER_BASE = "http://192.168.1.8:5000"; 
const std::string CSV_FILENAME = "Database.csv";

// PINOS (WiringPi: 1=GPIO18, 4=GPIO23)
#define LDR_PIN 1   
#define RELAY_PIN 4 

using json = nlohmann::json;
using namespace cv;
using namespace std;
using namespace zbar;

// GLOBAIS
std::mutex mtx; 
std::string shared_status = "cheio";
std::string shared_photo = "";
bool send_photo_flag = false;

std::atomic<int> threshold(1000); 
std::atomic<int> light_val(0);
std::atomic<bool> auto_mode(true); 
std::atomic<bool> manual_on(false);
std::atomic<bool> running(true);
std::atomic<bool> take_photo(false);

// LEITURA LUZ & RELE
void thread_light() {
    pinMode(RELAY_PIN, OUTPUT); digitalWrite(RELAY_PIN, LOW);
    while(running) {
        int r = 0;
        pinMode(LDR_PIN, OUTPUT); digitalWrite(LDR_PIN, LOW); delay(10);
        pinMode(LDR_PIN, INPUT);
        while (digitalRead(LDR_PIN) == LOW && r < 30000) { r++; delayMicroseconds(1); }
        light_val = r;

        bool state = (auto_mode) ? (light_val > threshold) : manual_on;
        digitalWrite(RELAY_PIN, state ? HIGH : LOW);
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    digitalWrite(RELAY_PIN, LOW);
}

// REDE
size_t write_cb(void* c, size_t s, size_t n, void* u) { ((string*)u)->append((char*)c, s*n); return s*n; }

void thread_net() {
    while(running) {
        string status, photo; bool do_photo = false;
        { lock_guard<mutex> l(mtx); status=shared_status; if(send_photo_flag){ photo=shared_photo; do_photo=true; send_photo_flag=false; } }

        if(do_photo) {
            CURL *c = curl_easy_init();
            if(c) {
                curl_mime *form = curl_mime_init(c);
                curl_mimepart *field = curl_mime_addpart(form);
                curl_mime_name(field, "file"); curl_mime_filedata(field, photo.c_str());
                curl_easy_setopt(c, CURLOPT_URL, (SERVER_BASE + "/api/upload_photo/" + MODULO_ID).c_str());
                curl_easy_setopt(c, CURLOPT_MIMEPOST, form);
                curl_easy_perform(c); curl_mime_free(form); curl_easy_cleanup(c);
            }
        }

        CURL* c = curl_easy_init();
        if(c) {
            json j; j["modulo_id"]=MODULO_ID; j["status"]=status; j["light_reading"]=(int)light_val;
            string js = j.dump(); string buf;
            struct curl_slist* h = NULL; h=curl_slist_append(h, "Content-Type: application/json");
            
            curl_easy_setopt(c, CURLOPT_URL, (SERVER_BASE + "/api/status").c_str());
            curl_easy_setopt(c, CURLOPT_HTTPHEADER, h);
            curl_easy_setopt(c, CURLOPT_POSTFIELDS, js.c_str());
            curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, write_cb);
            curl_easy_setopt(c, CURLOPT_WRITEDATA, &buf);
            curl_easy_setopt(c, CURLOPT_TIMEOUT, 3L);
            
            if(curl_easy_perform(c) == CURLE_OK) {
                try {
                    auto r = json::parse(buf);
                    if(r.contains("light_threshold")) threshold = r["light_threshold"];
                    if(r.contains("auto_mode")) auto_mode = r["auto_mode"];
                    if(r.contains("relay_on")) manual_on = r["relay_on"];
                    if(r.contains("photo_command") && r["photo_command"]) take_photo = true;
                } catch(...) {}
            }
            curl_slist_free_all(h); curl_easy_cleanup(c);
        }
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
}

// MAIN
int main() {
    if (wiringPiSetup() == -1) return 1;
    
    std::thread t1(thread_light);
    std::thread t2(thread_net);

    VideoCapture cap(0, CAP_V4L2); 
    cap.set(CAP_PROP_FRAME_WIDTH, 640); cap.set(CAP_PROP_FRAME_HEIGHT, 480);
    if(!cap.isOpened()) cap.open(0);
    
    ImageScanner scanner;
    scanner.set_config(ZBAR_NONE, ZBAR_CFG_ENABLE, 0);
    scanner.set_config(ZBAR_QRCODE, ZBAR_CFG_ENABLE, 1);

    string curr = "cheio";
    Mat frame, gray;
    auto last_log = chrono::steady_clock::now();

    while(running) {
        cap >> frame;
        if (frame.empty()) { std::this_thread::sleep_for(std::chrono::milliseconds(100)); continue; }

        if(take_photo) {
            string fn = "snap.jpg"; imwrite(fn, frame);
            { lock_guard<mutex> l(mtx); shared_photo=fn; send_photo_flag=true; }
            take_photo = false;
        }

        cvtColor(frame, gray, COLOR_BGR2GRAY);
        int w = gray.cols; int h = gray.rows;
        uchar *raw = (uchar *)gray.data;
        Image img(w, h, "Y800", raw, w * h);
        scanner.scan(img);
        
        bool q1=false, q2=false;
        for(Image::SymbolIterator s = img.symbol_begin(); s != img.symbol_end(); ++s) {
            if(s->get_data() == "q1") q1=true;
            if(s->get_data() == "q2") q2=true;
        }
        
        if (curr == "cheio") { if (q1) curr="metade"; else if (q2) curr="vazio"; }
        else if (curr == "metade") { if (!q1 && !q2) curr="cheio"; else if (q2) curr="vazio"; }
        else if (curr == "vazio") { if (!q1 && !q2) curr="cheio"; }

        { lock_guard<mutex> l(mtx); shared_status=curr; }

        auto now = chrono::steady_clock::now();
        if (chrono::duration_cast<chrono::milliseconds>(now - last_log).count() > 2000) {
            cout << "St: [" << curr << "] Luz: " << light_val << " Auto: " << auto_mode << endl;
            last_log = now;
        }
        img.set_data(NULL, 0);
    }
    return 0;
}