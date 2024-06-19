from brownie import accounts, SimpleStorage, config #o config é só para a hipotese de puxar o arquivo do .env

def simple_storage():
    account = accounts[0] #puxa a primira conta disponibilada pelo ganache
    # account = accounts.load("mytestaccount")  ==== jeito mais seguro
    # account = accounts.add(config["wallets"]["from_keys"]) puxa a conta que está no .env partir do brownie-config.yaml
    simple_storage = SimpleStorage.deploy({"from": account}) #faz o deploy do SimpleStorage com a conta escolhida, no caso a do ganache
    stored_value = simple_storage.retrieve()
    print(stored_value)
    transaction = simple_storage.store(15, {"from": account})
    transaction.wait(1)
    updated_store_value = simple_storage.retrieve()
    print(updated_store_value)


    
def main():
    simple_storage()

