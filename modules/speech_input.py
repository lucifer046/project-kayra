import speech_recognition as sr 
import sys 

# Function to convert the Speech to Text 
def live_speech_to_text():
    # Initialize the speech recognizer.
    recognizer = sr.Recognizer()

    # Use the microphone as the audio source. 
    with sr.Microphone() as source:
        print("Calibrating Microphone: Please wait for a second.")
        # Adjust the ambient background noise to improve accuracy. 
        recognizer.adjust_for_ambient_noise(source, duration=1.5)
        print("\n-- Calibration Complete. Start speakign! --")
        print("(Press ctrl+c to stop.)\n")

        # Continuous listning loop. 
        while True:
            try: 
                # Listen the audio
                # phrase_time_limit ensures to process this in chunks i.e. every 5 seconds 
                audio = recognizer.listen(source, timeout=None, phrase_time_limit=5)

                # Visual indicator that processing is happenng 
                sys.stdout.write("Processing...\r")
                sys.stdout.flush()

                # Convert speech to text using google's free API
                text = recognizer.recognize_google(audio)

                # Clear the "Processing..." text and print the result 
                sys.stdout.write("         \r")
                sys.stdout.flush()
                print(f"User said: {text}")
                    
            except sr.UnknownValueError:
                # Trigger if the user is quite or the speaker is entirely  unintelligible 
                pass
            except sr.RequestError as e:
                print(f"\n [Error] Could not request the results from Google API; {e}")
            except KeyboardInterrupt:
                print("\nStopping live STT. GoodByee! ")

if __name__ == "__main__":
    live_speech_to_text()
    