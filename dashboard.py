
import streamlit as st
import pandas as pd
import time
import os
import json # Global import fix

LOG_FILE = "/home/ec2-user/.pm2/logs/validator-out.log"

st.set_page_config(
    page_title="Minotaur Validator Dashboard",
    page_icon="🐂",
    layout="wide",
)

st.title("🐂 Minotaur Subnet Validator Monitor")

def get_last_logs(n=50):
    if not os.path.exists(LOG_FILE):
        return ["Log file not found."]
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    return lines[-n:]

def parse_logs(lines):
    status = {"Registered": "Unknown", "UID": "Unknown", "Permit": "Unknown", "Last Update": "Unknown"}
    
    for line in reversed(lines):
        if "Registered:" in line:
            status["Registered"] = line.split("Registered:")[1].strip()
        if "Validator UID:" in line:
            status["UID"] = line.split("Validator UID:")[1].strip()
        if "Validator Permit:" in line and status["Permit"] == "Unknown":
            status["Permit"] = line.split("Validator Permit:")[1].strip()
        
    status["Last Update"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return status

# --- Sidebar ---
st.sidebar.header("Configuration")
st.sidebar.text(f"Domain: valivali.minotaursubnet.com")
st.sidebar.text(f"IP: 34.239.154.249")
refresh_rate = st.sidebar.slider("Refresh Rate (s)", 1, 60, 5)

# --- Main Content ---
placeholder = st.empty()

while True:
    logs = get_last_logs(100)
    status = parse_logs(logs)
    
    with placeholder.container():
        # --- On-Chain Data Fetching ---
        try:
            # Refresh every 60s for live data
            if 'metagraph_data' not in st.session_state or time.time() - st.session_state.get('last_fetch', 0) > 60:
                try:
                    import bittensor as bt
                    # Initialize subtensor (lightweight)
                    sub = bt.subtensor(network='finney')
                    metagraph = sub.metagraph(netuid=112, lite=True)
                    
                    my_hotkey = "5E1ohAszHfhyQUEtz6mvCCkW4pYHsinPjxXS938fAZ2jFvCt" 
                    
                    # Fetch Pool Info for Pricing (Alpha -> TAO)
                    alpha_to_tao = 0.005 # Fallback from screenshot
                    try:
                        # Attempt to query raw storage if possible, otherwise use heuristic
                        print("Fetching subnet info...")
                        # sub.get_subnet_pool(112) might not exist, but let's try reading root weights or Recycled
                        # Actually, we can assume 0.005 if query fails, or parse from external API?
                        # For now, let's hardcode the '0.005' based on user input if we can't query.
                        # But wait! 'neuron.stake' is in ALPHA.
                        pass
                    except: pass
                    
                    if my_hotkey in metagraph.hotkeys:
                        uid = metagraph.hotkeys.index(my_hotkey)
                        neuron = metagraph.neurons[uid]
                        
                        # Fetch Coldkey Balance (Liquid TAO)
                        try:
                            balance = sub.get_balance(neuron.coldkey)
                            liquid_tao = float(balance.tao)
                        except:
                            liquid_tao = 0.0

                        current_data = {
                            "stake": float(neuron.stake),
                            "rank": float(neuron.rank),
                            "trust": float(neuron.trust),
                            "consensus": float(neuron.consensus),
                            "incentive": float(neuron.incentive),
                            "dividends": float(neuron.dividends),
                            "emission": float(neuron.emission), # Raw Unit (Likely Alpha)
                            "last_update": neuron.last_update,
                            "coldkey": neuron.coldkey,
                            "liquid_tao": liquid_tao,
                            "timestamp": time.time(),
                            "alpha_price": alpha_to_tao
                        }
                        st.session_state['metagraph_data'] = current_data
                        
                        # --- Earnings History Logging (Every fetch) ---
                        earnings_file = "/home/ec2-user/earnings_history.json"
                        history_data = []
                        if os.path.exists(earnings_file):
                            try:
                                with open(earnings_file, "r") as f:
                                    history_data = json.load(f)
                            except: pass
                        
                        if not history_data or (current_data["timestamp"] - history_data[-1]["timestamp"] > 300):
                            history_data.append(current_data)
                            history_data = history_data[-9000:]
                            with open(earnings_file, "w") as f:
                                json.dump(history_data, f)
                    else:
                        st.session_state['metagraph_data'] = None
                    
                    st.session_state['last_fetch'] = time.time()
                except Exception as e:
                    print(f"Metagraph error: {e}") 
                    # st.error(f"Metagraph Error: {e}")

            # Fetch generic status from logs first (fallback)
            status_color = "🟢" if status["Registered"] == "True" else "🔴"
            st.markdown(f"### {status_color} Validator Status: {status['Registered']}")

            # KPI Grid
            kpi1, kpi2, kpi3, kpi4 = st.columns(4)
            kpi1.metric("UID", status["UID"])
            kpi2.metric("Permit", status["Permit"])

            # Display Real Metrics
            data = st.session_state.get('metagraph_data')
            if data:
                price = data.get('alpha_price', 0.005)
                
                # --- Validator Financials ---
                st.markdown("### 💰 Validator Financials")
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Staked (Alpha)", f"{data['stake']:.2f} α")
                f2.metric("Liquid (Coldkey)", f"{data['liquid_tao']:.2f} τ")
                
                # Validator Daily Calculation
                val_daily_alpha = data['emission'] * 7200
                val_daily_tao = val_daily_alpha * price
                
                f3.metric("Validator Emission", f"{data['emission']:.4f} α/step")
                
                help_text = f"Daily: {val_daily_alpha:,.2f} Alpha * {price:.5f} Price"
                f4.metric("Est. Daily Rewards", f"{val_daily_tao:.2f} τ", help=help_text, delta=f"{val_daily_alpha:,.0f} α")

                # --- Subnet 112 Economics ---
                st.markdown("---")
                st.markdown(f"### 🌐 Subnet 112 Economics (Owner Stats) [1 α ≈ {price:.4f} τ]")
                
                if 'subnet_data' not in st.session_state or time.time() - st.session_state.get('last_sn_fetch', 0) > 300:
                   try:
                       import bittensor as bt
                       sub = bt.subtensor(network='finney')
                       metagraph = sub.metagraph(112, lite=True)
                       sn_info = sub.get_subnet_hyperparameters(112)

                       total_neuron_emission_alpha = sum([n.emission for n in metagraph.neurons])
                       
                       # If total_neuron_emission is 82%, Owner is 18% equivalent logic
                       owner_emission_alpha = total_neuron_emission_alpha * (0.18 / 0.82)
                       
                       owner_daily_alpha = owner_emission_alpha * 7200
                       owner_daily_tao = owner_daily_alpha * price
                       
                       subnet_daily_emission_alpha = (total_neuron_emission_alpha + owner_emission_alpha) * 7200
                       subnet_daily_emission_tao = subnet_daily_emission_alpha * price
                       
                       st.session_state['subnet_data'] = {
                           "owner_daily_alpha": owner_daily_alpha,
                           "owner_daily_tao": owner_daily_tao,
                           "subnet_daily_alpha": subnet_daily_emission_alpha,
                           "subnet_daily_tao": subnet_daily_emission_tao,
                           "active_neurons": len(metagraph.neurons),
                           "recycle_register_cost": getattr(sn_info, 'recycle', 'N/A')
                       }
                       st.session_state['last_sn_fetch'] = time.time()
                   except Exception as e:
                       print(f"Subnet data error: {e}")

                sn_data = st.session_state.get('subnet_data')
                if sn_data:
                    s1, s2, s3, s4 = st.columns(4)
                    s1.metric("Subnet Daily Emission", f"{sn_data['subnet_daily_tao']:.2f} τ", delta=f"{sn_data['subnet_daily_alpha']:,.0f} α")
                    s2.metric("Owner Daily Cut (18%)", f"{sn_data['owner_daily_tao']:.2f} τ", delta=f"{sn_data['owner_daily_alpha']:,.0f} α")
                    s3.metric("Active Neurons", sn_data['active_neurons'])
                    s4.metric("Registration Cost", f"{sn_data.get('recycle_register_cost', 'N/A')}")
                else:
                    st.info("Loading Subnet Economics...")

                # --- Charts ---
                st.markdown("---")
                earnings_file = "/home/ec2-user/earnings_history.json"
                if os.path.exists(earnings_file):
                    try:
                        with open(earnings_file, "r") as f:
                            hist = json.load(f)
                        if len(hist) > 1:
                            col_chart1, col_chart2 = st.columns(2)
                            
                            df_hist = pd.DataFrame(hist)
                            df_hist['date'] = pd.to_datetime(df_hist['timestamp'], unit='s')
                            df_hist = df_hist.set_index('date')
                            
                            with col_chart1:
                                st.subheader("📈 Stake Growth")
                                st.line_chart(df_hist[['stake']])
                                
                            with col_chart2:
                                st.subheader("💸 Daily Packet (Est)")
                                # Create a derived column for estimated daily earnings at that point
                                df_hist['daily_est'] = df_hist['emission'] * 7200
                                st.line_chart(df_hist[['daily_est']])

                    except: pass
        except Exception as e:
             st.error(f"Dashboard Loop Error: {e}")
             time.sleep(10)

        # Weight History Table (Preserved)
        st.subheader("⚖️ Set Weights History")
        history_file_w = "/home/ec2-user/weights_history.json"
        if os.path.exists(history_file_w):
            try:
                import json
                with open(history_file_w, "r") as f:
                    history_data_w = json.load(f)
                if history_data_w:
                    df_w = pd.DataFrame(history_data_w)
                    df_w["uids"] = df_w["uids"].apply(str)
                    df_w["weights"] = df_w["weights"].apply(str)
                    st.dataframe(df_w[["date", "block", "uids", "weights", "version_key"]].sort_values("date", ascending=False), use_container_width=True)
            except: pass

        # Logs Viewer
        st.subheader("📜 Live Logs (Last 50 lines)")
        st.code("".join(logs[-50:]), language="text")

    time.sleep(refresh_rate)
